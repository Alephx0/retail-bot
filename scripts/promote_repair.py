"""Activate or roll back a validated package only while this data workspace is stopped."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from retail.task_groups.workspace_lock import WorkspaceLock
from retail.source_repair import SOURCE


def activate(package,repo,data_dir, *, rollback=False):
    package=Path(package).resolve();repo=Path(repo).resolve()
    value=json.loads(package.read_text(encoding='utf-8'))
    report=value['report']
    if report['status']!='ready_for_review' or not report['regression_failed_before'] or not report['regression_passed_after'] or not report['performance']['accepted'] or not report['integration']:
        raise ValueError('Package has not passed all deployment gates')
    import re
    files=value['files'];before=value['before']
    if set(before)!={SOURCE} or SOURCE not in files or any(n!=SOURCE and not re.fullmatch(r'tests/test_recovery_generated_[a-z0-9_]+\.py',n) for n in files):
        raise ValueError('Package contains unsupported source paths')
    if any(hashlib.sha256(content.encode()).hexdigest()!=report.get('file_sha256',{}).get(name) for name,content in files.items()) or hashlib.sha256(before[SOURCE].encode()).hexdigest()!=report.get('before_sha256'):
        raise ValueError('Package contents changed after validation')
    # WorkspaceLock is the same exclusive OS lock acquired by the application.
    with_lock=WorkspaceLock(Path(data_dir))
    try:
        if (repo/SOURCE).read_text(encoding='utf-8')!=(files[SOURCE] if rollback else before[SOURCE]):
            raise ValueError('Destination changed since validation; revalidate the repair')
        if not rollback and any((repo/n).exists() for n in files if n!=SOURCE):
            raise ValueError('Regression destination already exists')
        if rollback and any((repo/n).exists() and (repo/n).read_text(encoding='utf-8')!=files[n] for n in files if n!=SOURCE):
            raise ValueError('Generated regression changed; preserve it for manual rollback review')
        targets={SOURCE:before[SOURCE]} if rollback else files
        originals={name:(repo/name).read_bytes() if (repo/name).exists() else None for name in targets}
        try:
            for name,content in targets.items():
                path=(repo/name).resolve()
                if repo not in path.parents:raise ValueError('Invalid destination')
                path.write_text(content,encoding='utf-8')
        except BaseException:
            for name,content in originals.items():
                if content is not None:(repo/name).write_bytes(content)
                elif (repo/name).exists():(repo/name).unlink()
            raise
        if rollback:
            for name in files:
                if name!=SOURCE and (repo/name).exists():(repo/name).unlink()
        audit={'action':'rollback' if rollback else 'activate','package_sha256':hashlib.sha256(package.read_bytes()).hexdigest(),'files':list(targets)}
        history=package.parent/'deployments.jsonl'
        with history.open('a',encoding='utf-8') as stream:stream.write(json.dumps(audit)+'\n')
        return audit
    finally:with_lock.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--package',required=True,type=Path);p.add_argument('--repo',required=True,type=Path);p.add_argument('--data-dir',required=True,type=Path);p.add_argument('--rollback',action='store_true')
    a=p.parse_args();print(json.dumps(activate(a.package,a.repo,a.data_dir,rollback=a.rollback)))
