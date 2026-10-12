import hashlib
import json
from pathlib import Path

import pytest

from retail.source_repair import SOURCE,checked_proposal
from retail.task_groups.workspace_lock import WorkspaceLock
from scripts.promote_repair import activate


def proposal():
    return dict(before="r'^add to (?:shopping )?cart$'",after="r'^add (?:this product )?to (?:shopping )?cart$'",
        test_name='tests/test_recovery_generated_cart.py',test_source='def test_repair():\n    assert True\n')


def test_repair_cannot_change_guards_or_existing_tests():
    source=Path(SOURCE).read_text(encoding='utf-8').replace("r'^add (?:this product )?to (?:shopping )?cart$'","r'^add to (?:shopping )?cart$'")
    changed,name,test=checked_proposal(source,proposal(),'ADD_TO_CART')
    assert changed!=source
    for update in ({'before':'async def resolve(page, action):','after':'async def resolve(page, action):\n    return None'},
                   {'after':"r'^.*$'"},{'test_name':'tests/test_checkout.py'},
                   {'test_source':'import os\ndef test_escape():\n    assert os.environ\n'}):
        with pytest.raises(ValueError):checked_proposal(source,{**proposal(),**update},'ADD_TO_CART')


def test_activation_checks_stopped_workspace_content_and_rollback(tmp_path):
    repo=tmp_path/'source';(repo/'retail').mkdir(parents=True);(repo/'tests').mkdir()
    original=Path(SOURCE).read_text(encoding='utf-8').replace("r'^add (?:this product )?to (?:shopping )?cart$'","r'^add to (?:shopping )?cart$'");modified,name,test=checked_proposal(original,proposal(),'ADD_TO_CART')
    (repo/SOURCE).write_text(original,encoding='utf-8');files={SOURCE:modified,name:test}
    report={'status':'ready_for_review','regression_failed_before':True,'regression_passed_after':True,
            'performance':{'accepted':True},'integration':True,'file_sha256':{n:hashlib.sha256(c.encode()).hexdigest() for n,c in files.items()},
            'before_sha256':hashlib.sha256(original.encode()).hexdigest()}
    package=tmp_path/'package.json';package.write_text(json.dumps({'report':report,'files':files,'before':{SOURCE:original}}))
    data=tmp_path/'data';lock=WorkspaceLock(data)
    try:
        with pytest.raises((ValueError,RuntimeError,OSError)):activate(package,repo,data)
    finally:lock.close()
    assert (repo/SOURCE).read_text(encoding='utf-8')==original
    assert activate(package,repo,data)['action']=='activate'
    assert (repo/SOURCE).read_text(encoding='utf-8')==modified
    assert activate(package,repo,data,rollback=True)['action']=='rollback'
    assert (repo/SOURCE).read_text(encoding='utf-8')==original
    assert not (repo/name).exists()
    value=json.loads(package.read_text());value['files'][SOURCE]+='\n# unexpected change';package.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='contents changed'):activate(package,repo,data)
