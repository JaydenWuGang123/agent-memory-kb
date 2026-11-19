#!/usr/bin/env python3
"""Backup, Git snapshot, iCloud encryption mirror, private remote push, and alerting helpers."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows lacks fcntl
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

CST_TZ = timezone(timedelta(hours=8), 'CST')

from kb_filters import atomic_write_text

DEFAULT_BUNDLE_BACKUP_PATH = Path.home() / '.local' / 'state' / 'agent-memory-kb' / 'kb-git-mirror.bundle'
DEFAULT_ICLOUD_BUNDLE_PATH = Path.home() / 'Library' / 'Mobile Documents' / 'com~apple~CloudDocs' / 'agent-memory-kb-backup' / 'kb-git-mirror.bundle'
KEYCHAIN_ENC_SERVICE = os.environ.get('AGENT_MEMORY_KB_KEYCHAIN_SERVICE', 'agent-memory-kb.icloud-bundle')


def load_kb_config(root: Path, defaults: dict | None = None) -> dict:
    cfg = dict(defaults or {})
    cfg.setdefault('git_gc_loose_threshold', 300)
    cfg.setdefault('icloud_lag_warn_hours', 4)
    cfg.setdefault('restore_verify_interval_hours', 24)
    cfg.setdefault('profile_auto_archive_lines', 260)
    cfg.setdefault('staleness_warn_days', 14)
    cfg.setdefault('macos_notify_on_error', True)
    cfg.setdefault('alert_cooldown_hours', 12)
    cfg.setdefault('private_git_remote', None)

    cfg_path = root / '_同步' / 'kb_config.json'
    if cfg_path.is_file():
        try:
            loaded = json.loads(cfg_path.read_text(encoding='utf-8'))
            for key, val in loaded.items():
                if key in cfg or key in (
                    'active_repos', 'cwd_categories', 'title_categories',
                    'synonyms', 'noise_filter',
                ):
                    cfg[key] = val
        except Exception:
            pass
    return cfg


def load_pipeline_state(root: Path) -> dict:
    p = root / '_同步' / 'pipeline-state.json'
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_pipeline_state(root: Path, state: dict) -> None:
    p = root / '_同步' / 'pipeline-state.json'
    atomic_write_text(p, json.dumps(state, ensure_ascii=False, indent=2) + '\n')


@contextmanager
def sync_lock(root: Path):
    sync_dir = root / '_同步'
    sync_dir.mkdir(parents=True, exist_ok=True)
    lock_path = sync_dir / '.kb-sync.lock'
    with lock_path.open('a+', encoding='utf-8') as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - Windows file locking
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                f.write(' ')
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        else:  # pragma: no cover
            yield


def ensure_pre_commit_hook(root: Path):
    hooks_dir = root / '.git' / 'hooks'
    if not hooks_dir.is_dir():
        return
    hook_path = hooks_dir / 'pre-commit'
    hook_body = (
        "#!/bin/sh\n"
        "set -e\n"
        "REPO_ROOT=\"$(git rev-parse --show-toplevel)\"\n"
        "PY=python3\n"
        "command -v python3 >/dev/null 2>&1 || PY=python\n"
        "\"$PY\" \"$REPO_ROOT/_同步/kb_doctor.py\" --root \"$REPO_ROOT\" --ignore-budget --skip-external-checks >/dev/null\n"
    )
    if not hook_path.is_file() or hook_path.read_text(encoding='utf-8', errors='replace') != hook_body:
        hook_path.write_text(hook_body, encoding='utf-8')
        try:
            hook_path.chmod(0o755)
        except OSError:
            pass


def maybe_git_gc(root: Path, threshold: int) -> bool:
    res = subprocess.run(['git', '-C', str(root), 'count-objects'], capture_output=True, text=True, check=False)
    m = re.search(r'(\d+)\s+objects,', res.stdout or '')
    if m and int(m.group(1)) > threshold:
        subprocess.run(['git', '-C', str(root), 'gc', '--quiet'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    return False


def _get_keychain_passphrase(service: str) -> str | None:
    if os.environ.get('PERSONAL_KB_SKIP_KEYCHAIN') == '1':
        return None
    env_pw = (os.environ.get('AGENT_MEMORY_KB_ENC_PASS') or '').strip()
    if env_pw:
        return env_pw
    if sys.platform != 'darwin':
        return None
    try:
        res = subprocess.run(
            ['security', 'find-generic-password', '-s', service, '-w'],
            capture_output=True, text=True, timeout=3, check=False
        )
        pw = (res.stdout or '').strip()
        return pw if (res.returncode == 0 and pw) else None
    except Exception:
        return None


def get_git_head(root: Path) -> str | None:
    res = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=False)
    return (res.stdout or '').strip() if res.returncode == 0 else None


def atomic_create_local_bundle(root: Path, bundle_path: Path) -> bool:
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_bundle = bundle_path.with_name(f".{bundle_path.name}.tmp.{os.getpid()}")
    tmp_bundle.unlink(missing_ok=True)

    create_res = subprocess.run(
        ['git', '-C', str(root), 'bundle', 'create', str(tmp_bundle), '--all'],
        capture_output=True, text=True, check=False
    )
    if create_res.returncode != 0 or not tmp_bundle.is_file():
        tmp_bundle.unlink(missing_ok=True)
        raise RuntimeError(f"git bundle create 失败: {create_res.stderr or create_res.stdout}")

    verify_res = subprocess.run(
        ['git', '-C', str(root), 'bundle', 'verify', str(tmp_bundle)],
        capture_output=True, text=True, check=False
    )
    if verify_res.returncode != 0:
        tmp_bundle.unlink(missing_ok=True)
        raise RuntimeError(f"git bundle verify 校验失败: {verify_res.stderr or verify_res.stdout}")

    os.replace(tmp_bundle, bundle_path)
    return True


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _find_openssl() -> str:
    found = shutil.which('openssl')
    if found:
        return found
    git_openssl = Path(r'C:\Program Files\Git\usr\bin\openssl.exe')
    if git_openssl.is_file():
        return str(git_openssl)
    return 'openssl'


def sync_icloud_mirror(root: Path, bundle_path: Path, icloud_path: Path | None, passphrase_override: str | None = None) -> tuple[bool, str | None, bool]:
    """Copy or AES-256 encrypt local bundle to iCloud Drive, verifying decrypt round-trip before commit."""
    if icloud_path is None:
        return False, None, False
    if sys.platform != 'darwin' and icloud_path == DEFAULT_ICLOUD_BUNDLE_PATH:
        return False, None, False
    passphrase = passphrase_override if passphrase_override is not None else _get_keychain_passphrase(KEYCHAIN_ENC_SERVICE)
    encrypted = bool(passphrase)
    target_icloud = icloud_path.with_suffix('.bundle.enc') if encrypted else icloud_path
    tmp_icloud = target_icloud.with_name(f".{target_icloud.name}.tmp.{os.getpid()}")
    try:
        target_icloud.parent.mkdir(parents=True, exist_ok=True)
        if encrypted:
            openssl_bin = _find_openssl()
            enc_res = subprocess.run(
                [openssl_bin, 'enc', '-aes-256-cbc', '-pbkdf2', '-salt', '-in', str(bundle_path), '-out', str(tmp_icloud), '-pass', 'stdin'],
                input=passphrase + '\n', text=True, capture_output=True, check=False
            )
            if enc_res.returncode != 0:
                raise RuntimeError(enc_res.stderr or 'openssl enc failed')
            with tempfile.TemporaryDirectory() as td:
                dec_check = Path(td) / 'verify.bundle'
                dec_res = subprocess.run(
                    [openssl_bin, 'enc', '-d', '-aes-256-cbc', '-pbkdf2', '-in', str(tmp_icloud), '-out', str(dec_check), '-pass', 'stdin'],
                    input=passphrase + '\n', text=True, capture_output=True, check=False
                )
                if dec_res.returncode != 0 or not dec_check.is_file():
                    raise RuntimeError('encrypted bundle decrypt check failed')
                v_res = subprocess.run(
                    ['git', '-C', str(root), 'bundle', 'verify', str(dec_check)],
                    capture_output=True, text=True, check=False
                )
                if v_res.returncode != 0:
                    raise RuntimeError('encrypted bundle git verify failed')
        else:
            shutil.copy2(bundle_path, tmp_icloud)
        os.replace(tmp_icloud, target_icloud)
        if encrypted and icloud_path.is_file():
            _safe_unlink(icloud_path)
        for orphan in target_icloud.parent.glob(f".{target_icloud.name}.tmp.*"):
            _safe_unlink(orphan)
        return True, None, encrypted
    except PermissionError as exc:
        _safe_unlink(tmp_icloud)
        return False, f"iCloud 后台复制受 macOS TCC 限制 ({exc.strerror or 'PermissionError'})，本地校验镜像已更新", encrypted
    except Exception as exc:
        _safe_unlink(tmp_icloud)
        return False, f"iCloud 复制异常: {str(exc)[:160]}", encrypted


def atomic_create_and_verify_bundle(root: Path, bundle_path: Path, icloud_path: Path | None) -> tuple[bool, bool, str | None]:
    atomic_create_local_bundle(root, bundle_path)
    icloud_synced, icloud_note, _ = sync_icloud_mirror(root, bundle_path, icloud_path)
    return True, icloud_synced, icloud_note


def icloud_needs_catchup(bundle_path: Path, icloud_target: Path | None, prev_state: dict) -> bool:
    if icloud_target is None or not bundle_path.is_file():
        return False
    has_passphrase = bool(_get_keychain_passphrase(KEYCHAIN_ENC_SERVICE))
    expected_file = icloud_target.with_suffix('.bundle.enc') if has_passphrase else icloud_target
    try:
        if not expected_file.is_file():
            return True
        if expected_file.stat().st_mtime < bundle_path.stat().st_mtime - 2.0:
            return True
    except PermissionError:
        return True
    if prev_state.get('icloud_synced') is False:
        return True
    return False


def describe_icloud_status(
    bundle_path: Path,
    icloud_path: Path | None,
    warn_hours: float,
    prev_state: dict | None = None,
) -> tuple[str, str]:
    if icloud_path is None:
        return '已跳过（测试/显式关闭）', 'skipped'
    enc_path = icloud_path.with_suffix('.bundle.enc')
    try:
        has_enc = enc_path.is_file()
        has_plain = icloud_path.is_file()
    except PermissionError:
        return f'**⚠️ 后台无权访问 iCloud 目录**（macOS TCC 限制，需系统授权或终端运行 `~/.local/bin/personal-kb-sync.sh`：`{icloud_path}`）', 'blocked'

    if not has_enc and not has_plain:
        if prev_state and prev_state.get('icloud_note'):
            return f"**⚠️ 未生成**（{prev_state['icloud_note']}：`{icloud_path}`）", 'blocked'
        return f'未生成（目标 `{icloud_path}` 尚不存在）', 'missing'

    actual_icloud = enc_path if has_enc else icloud_path
    enc_tag = '（Keychain AES-256 加密）' if actual_icloud.suffix == '.enc' else '（未加密原生 bundle）'
    if not bundle_path.is_file():
        return f'`{actual_icloud}` {enc_tag}', 'ok'

    try:
        ic_mtime = actual_icloud.stat().st_mtime
        loc_mtime = bundle_path.stat().st_mtime
    except PermissionError:
        return f'**⚠️ 后台无权读取 iCloud 状态**（macOS TCC 限制：`{actual_icloud}`）', 'blocked'

    lag_sec = max(0.0, loc_mtime - ic_mtime)
    lag_hours = lag_sec / 3600.0
    prev_failed = bool(prev_state and prev_state.get('icloud_synced') is False and prev_state.get('icloud_note'))
    if prev_failed and ic_mtime <= loc_mtime + 2.0:
        fails = prev_state.get('icloud_consecutive_failures', 1)
        ic_ts = datetime.fromtimestamp(ic_mtime, CST_TZ).strftime('%Y-%m-%d %H:%M')
        return (
            f"**⚠️ 后台同步受阻**（连续 {fails} 次未同步：{prev_state['icloud_note']}；云盘副本停留在 `{ic_ts}`，滞后 {lag_hours:.1f} 小时，本地镜像已最新）{enc_tag}：`{actual_icloud}`",
            'blocked',
        )
    if lag_hours > warn_hours:
        return f'**⚠️ 滞后 {lag_hours:.1f} 小时** {enc_tag}（需在终端运行 `~/.local/bin/personal-kb-sync.sh` 补同步：`{actual_icloud}`）', 'lagging'
    return f'已同步（延迟 < {max(1, int(lag_sec // 60))} 分钟）{enc_tag}：`{actual_icloud}`', 'ok'


def maybe_push_private_remote(root: Path, remote_name: str | None) -> tuple[bool, str | None]:
    if not remote_name or not isinstance(remote_name, str):
        return False, None
    if 'public' in remote_name.lower():
        return False, '安全门禁拒绝向名称含 public 的远端推送完整私有库'
    url_res = subprocess.run(['git', '-C', str(root), 'remote', 'get-url', remote_name], capture_output=True, text=True, check=False)
    if url_res.returncode != 0:
        return False, f'未找到远端 `{remote_name}`'
    remote_url = (url_res.stdout or '').strip()
    if 'public' in remote_url.lower():
        return False, f'安全门禁拒绝向公共仓库 URL 推送完整私有库 ({remote_url})'
    env = os.environ.copy()
    env.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes -o ConnectTimeout=10')
    push_res = subprocess.run(
        ['git', '-C', str(root), 'push', '--prune', remote_name, 'refs/heads/*:refs/heads/*', 'refs/tags/*:refs/tags/*'],
        capture_output=True, text=True, timeout=120, env=env, check=False
    )
    if push_res.returncode != 0:
        return False, (push_res.stderr or push_res.stdout or 'git push failed').strip()[:200]
    return True, f'已镜像推送至私有远端 `{remote_name}`'


def maybe_notify_issues(cfg: dict, state: dict, icloud_alert_state: str, icloud_note: str | None, exporter_errors: list, doctor_res: dict, bundle_verified: bool, skip_notify: bool) -> None:
    if skip_notify or sys.platform != 'darwin' or not cfg.get('macos_notify_on_error', True):
        return
    issues: list[tuple[str, str]] = []
    if exporter_errors:
        issues.append(('exporter', f"导出脚本异常 ({len(exporter_errors)} 项): {exporter_errors[0].get('script')}"))
    if not doctor_res.get('healthy', True):
        issues.append(('doctor', '知识库健康巡检发现异常（死链/引用/脱敏门禁）'))
    if not bundle_verified:
        issues.append(('bundle', '本地 Git 灾备镜像校验未通过'))
    if state.get('restore_verified') is False:
        issues.append(('restore', f"灾备还原演练失败: {state.get('restore_error') or '请查看同步报告'}"))
    if cfg.get('private_git_remote') and state.get('private_remote_ok') is False:
        issues.append(('private_remote', f"私有 Git 远端备份失败: {state.get('private_remote_note') or '请检查网络或认证'}"))
    has_healthy_private_remote = bool(cfg.get('private_git_remote') and state.get('private_remote_ok') is True)
    if not has_healthy_private_remote:
        if icloud_alert_state == 'lagging' or (icloud_alert_state == 'blocked' and state.get('icloud_consecutive_failures', 0) >= 2):
            issues.append(('icloud', icloud_note or 'iCloud 异地备份滞后，请授权完全磁盘访问或终端运行同步脚本'))

    if not issues:
        return
    now_epoch = datetime.now(timezone.utc).timestamp()
    cooldown_sec = float(cfg.get('alert_cooldown_hours', 12)) * 3600.0
    last_alerts = state.setdefault('last_alerts', {})
    to_notify = [msg for key, msg in issues if now_epoch - float(last_alerts.get(key, 0.0)) >= cooldown_sec]
    if not to_notify:
        return
    for key, _ in issues:
        if now_epoch - float(last_alerts.get(key, 0.0)) >= cooldown_sec:
            last_alerts[key] = now_epoch
    title = '个人知识库运维告警'
    body = '；'.join(to_notify)[:180].replace('"', "'")
    try:
        subprocess.run(
            ['/usr/bin/osascript', '-e', f'display notification "{body}" with title "{title}"'],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
        )
    except Exception:
        pass
