import json
from pathlib import Path
import subprocess

import pytest
from scripts.repair_incident import apply_proposal, prepare, sanitize, validate


INCIDENT={'schema_version':1,'host':'www.amazon.com','route':'product','action':'ADD_TO_CART','category':'changed_control'}


def test_source_repair_sanitizes_and_deduplicates(tmp_path):
    repo=tmp_path/'source'; repo.mkdir()
    def git(*args):
        return subprocess.run(['git',*args],cwd=repo,check=True,capture_output=True)
    git('init'); git('config','user.email','fixture@example.test'); git('config','user.name','Fixture')
    (repo/'README.md').write_text('Controlled source')
    git('add','README.md'); git('commit','-m','fixture')
    folder=prepare(repo,{**INCIDENT,'password':'PRIVATE','url':'PRIVATE'},tmp_path/'repair')
    assert 'PRIVATE' not in (folder/'incident.json').read_text()
    assert json.loads((folder/'report.json').read_text())['production_changed'] is False
    assert not git('status','--porcelain').stdout
    with pytest.raises(ValueError,match='exists'):
        prepare(repo,INCIDENT,tmp_path/'repair')
    with pytest.raises(ValueError):
        sanitize({**INCIDENT,'host':'evil.example'})


def test_generated_patch_is_validated_before_any_file_write(tmp_path):
    source=tmp_path/'retail'; source.mkdir()
    target=source/'interactions.py'; target.write_text('original=True')
    valid={'summary':'fixture','needs_evidence':False,'files':[
        {'path':'retail/interactions.py','content':'fixed=True'},
        {'path':'tests/test_recovery_generated.py','content':'def test_fixed():\n    assert True\n'}]}
    for bad in ['../outside.py','retail/store.py','AGENTS.md','tests/test_checkout.py']:
        proposal={**valid,'files':[*valid['files'],{'path':bad,'content':'bad=True'}]}
        with pytest.raises(ValueError): apply_proposal(tmp_path,proposal)
        assert target.read_text()=='original=True'
    invalid={**valid,'files':[valid['files'][0],{'path':'tests/test_recovery_generated.py','content':'invalid code!'}]}
    with pytest.raises(SyntaxError): apply_proposal(tmp_path,invalid)
    assert target.read_text()=='original=True'
    apply_proposal(tmp_path,valid)
    assert target.read_text()=='fixed=True'
    with pytest.raises(ValueError,match='Existing regression tests'):
        apply_proposal(tmp_path,valid)


def test_missing_evidence_does_not_invent_source_fix(tmp_path):
    apply_proposal(tmp_path,{'summary':'need fixture','needs_evidence':True,'files':[]})
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError):
        apply_proposal(tmp_path,{'summary':'bad','needs_evidence':True,'files':[{'path':'retail/amazon.py','content':'pass'}]})


@pytest.mark.parametrize('performance_ok', [True, False])
def test_review_gate_runs_reproducer_regression_and_performance(tmp_path, monkeypatch, performance_ok):
    from types import SimpleNamespace
    import scripts.repair_incident as repair
    checkout=tmp_path/'repair'; (checkout/'retail').mkdir(parents=True); (checkout/'tests').mkdir()
    (checkout/'retail/interactions.py').write_text('fixed=True')
    (checkout/'tests/test_recovery_generated.py').write_text('def test_fix(): assert True')
    folder=checkout/'artifacts'/'source-repair'; folder.mkdir(parents=True)
    repair.write_json(folder/'report.json',{'worktree':str(checkout),'base':'abc','status':'proposed'})
    commands=[]
    def command(args, cwd, **kwargs):
        commands.append(args)
        result=''
        if args[:3]==['git','rev-parse','HEAD']: result='abc'
        elif args[:3]==['git','diff','--name-only']: result='retail/interactions.py'
        elif args[:3]==['git','ls-files','--others']: result='tests/test_recovery_generated.py'
        elif args[:2]==['git','show']: result='fixed=False'
        elif 'scripts/benchmark_recovery.py' in args:
            repair.write_json(folder/'performance.json',{'accepted':performance_ok})
        elif '-m' in args:
            assert (checkout/'retail/interactions.py').read_text()=='fixed=True'
            result='passed'
        return SimpleNamespace(stdout=result,returncode=0)
    monkeypatch.setattr(repair,'run',command)
    def baseline(*args,**kwargs):
        assert (checkout/'retail/interactions.py').read_text()=='fixed=False'
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(repair.subprocess,'run',baseline)
    if performance_ok:
        assert validate(folder)['status']=='ready_for_review'
    else:
        with pytest.raises(ValueError,match='p95'): validate(folder)
        assert json.loads((folder/'report.json').read_text())['status']=='rejected'
    assert (checkout/'retail/interactions.py').read_text()=='fixed=True'
    assert not any('merge' in c or 'push' in c for c in commands)
