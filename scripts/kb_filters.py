#!/usr/bin/env python3
"""Shared atomic I/O, multi-layer DLP credential redaction, and noise conversation filters."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re

SECRET_REDACT_RULES = [
    ("PEM 私钥", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")),
    ("JWT 令牌", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("Google OAuth client_secret", re.compile(r"GOCSPX-[A-Za-z0-9_-]{10,}")),
    ("Google OAuth access_token", re.compile(r"ya29\.[A-Za-z0-9_-]{20,}")),
    ("Google OAuth refresh_token", re.compile(r"(?<![A-Za-z0-9_/-])1//[A-Za-z0-9_-]{20,}")),
    ("GitHub Token", re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}")),
    ("OpenAI / Anthropic Key", re.compile(r"\bsk-(?:ant-[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{24,})\b")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("数据库连接串密码", re.compile(r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|amqp)://[^:\s/]+:[^@\s\[\]]{4,}@")),
    ("SSO / 会话 Cookie 令牌", re.compile(r"(?:\bsso-(?:epc)?token|\bchuangtzuweb|\bJSESSIONID|\bacw\\?_tc)\s*=\s*[A-Za-z0-9._~+/=-]{12,}", re.I)),
]

_JSON_SECRET_FIELD_RE = re.compile(
    r'("(?:client_secret|refresh_token|access_token|id_token|secret|api_key|apikey|password|token)"\s*:\s*")([^"\n]{10,})(")',
    re.I,
)
_ENV_SECRET_RE = re.compile(
    r'\b((?:EXPORT\s+)?[A-Z0-9_]*(?:SECRET_KEY|API_KEY|ACCESS_KEY|AUTH_TOKEN|PRIVATE_KEY|CLIENT_SECRET|REFRESH_TOKEN|ACCESS_TOKEN|DB_PASS(?:WORD)?|SIGNING_KEY|BOT_TOKEN)[A-Z0-9_]*\s*[=:]\s*["\']?)([A-Za-z0-9_./+=~-]{12,})(["\']?)'
)
_AUTH_BEARER_RE = re.compile(
    r'(\bAuthorization\*{0,2}\s*:\s*Bearer\s+)([A-Za-z0-9._~+/=-]{16,})',
    re.I,
)
_COOKIE_HEADER_RE = re.compile(
    r'(\b(?:Cookie|Set-Cookie)\*{0,2}\s*:\s*)([^\n]{18,})',
    re.I,
)
_DB_DSN_RE = re.compile(
    r'(\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|amqp)://[^:\s/]+:)([^@\s\[\]]{4,})(@)'
)
_PIPE_ACCOUNT_BUNDLE_RE = re.compile(
    r'(?:\[[^\]\n@]+@[^\]]+\]\([^)\n]+\)|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})'
    r'(?:\|(?:\[[^\]\n]+\]\([^)\n]+\)|[^\s|\]\[]+)){2,}'
)
_INLINE_ACCOUNT_PASS_RE = re.compile(
    r'(账号\s*[:：]\s*)([A-Za-z0-9._%+-]{3,})(\s+密码\s*[:：]\s*)([A-Za-z0-9!@#$%^&*+=_.-]{6,})(?=\s|$)'
)
_INLINE_PASSWORD_RE = re.compile(
    r'((?:密码|password|passwd|安全密码)\s*[:：=]\s*)([A-Za-z0-9!@#$%^&*+=_.-]{6,})(?=\s|$)',
    re.I,
)
_TOTP_BASE32_32_RE = re.compile(
    r'(?<![A-Za-z0-9_-])(?=[a-z2-7]{32}(?![A-Za-z0-9_-]))(?=.*[g-z])[a-z2-7]{32}(?![A-Za-z0-9_-])'
)
_IDCARD_18_RE = re.compile(r'(?<![0-9A-Za-z])([1-8]\d{5}(?:19[4-9]\d|20[0-2]\d)(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx])(?![0-9A-Za-z])')
_CONTEXT_HINT_RE = re.compile(r'(token|secret|apikey|api_key|bearer|cookie|credential|私钥|密钥|令牌|鉴权|密码)', re.I)
_CANDIDATE_TOKEN_RE = re.compile(r'(?<![A-Za-z0-9_./-])([A-Za-z0-9_/+=-]{28,})(?![A-Za-z0-9_./-])')
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
_HEX_RE = re.compile(r'^[0-9a-f]{24,}$', re.I)
_SAFE_PLACEHOLDERS = ("已脱敏", "example", "placeholder", "your_", "xxxx", "<token>", "<secret>")

DEFAULT_NOISE_FILTER = {
    "drop_zero_message": True,
    "probe_title_regex": r"^Reply (?:with )?exactly (?:[A-Z0-9_]+_)?OK\b",
    "trivial_user_regex": r"^(?:你好|您好|hi|hello|你是什么模型|测试一下你现在有没有啥问题)[!！?？。.,，\s]*$",
    "excluded_ids": [],
}


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Atomically write text file via PID-scoped temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp_path.write_text(content, encoding=encoding)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _is_valid_cn_idcard(val: str) -> bool:
    """Verify 18-digit GB 11643-1999 checksum so random 18-digit IDs do not false-positive."""
    if len(val) != 18:
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    check_map = "10X98765432"
    try:
        total = sum(int(val[i]) * weights[i] for i in range(17))
    except ValueError:
        return False
    return check_map[total % 11] == val[17].upper()


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = float(len(s))
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _is_high_entropy_secret(token: str) -> bool:
    if len(token) < 28 or _UUID_RE.match(token) or _HEX_RE.match(token):
        return False
    if token.startswith("call_") or (token.startswith("/") and "=" not in token):
        return False
    if token.count("-") >= 2 and len(re.findall(r"[A-Za-z]{4,}", token)) >= 2 and not re.search(r"\d{4,}", token) and "=" not in token:
        return False
    low = token.lower()
    if any(p in low for p in _SAFE_PLACEHOLDERS):
        return False
    classes = sum([
        any(c.islower() for c in token),
        any(c.isupper() for c in token),
        any(c.isdigit() for c in token),
        any(c in "_/+=-" for c in token),
    ])
    return classes >= 3 and _shannon_entropy(token) >= 3.8


def redact_secrets(text: str) -> str:
    """Scrub OAuth tokens, JWTs, API keys, auth headers, DB DSNs, valid ID cards, and pasted secrets."""
    if not text:
        return text

    def _mask_env(m: re.Match) -> str:
        val = m.group(2)
        if any(p in val.lower() for p in _SAFE_PLACEHOLDERS):
            return m.group(0)
        return f"{m.group(1)}[已脱敏凭证]{m.group(3)}"

    def _mask_json(m: re.Match) -> str:
        val = m.group(2)
        if any(p in val.lower() for p in _SAFE_PLACEHOLDERS) or val.startswith(("/", "http")):
            return m.group(0)
        return f"{m.group(1)}[已脱敏凭证]{m.group(3)}"

    def _mask_cookie(m: re.Match) -> str:
        val = m.group(2)
        if "=" in val and "[已脱敏" not in val:
            return f"{m.group(1)}[已脱敏Cookie]"
        return m.group(0)

    out = _JSON_SECRET_FIELD_RE.sub(_mask_json, text)
    out = _ENV_SECRET_RE.sub(_mask_env, out)
    out = _AUTH_BEARER_RE.sub(lambda m: m.group(0) if "已脱敏" in m.group(2) else f"{m.group(1)}[已脱敏令牌]", out)
    out = _COOKIE_HEADER_RE.sub(_mask_cookie, out)
    out = _DB_DSN_RE.sub(r"\1[已脱敏密码]\3", out)
    out = _PIPE_ACCOUNT_BUNDLE_RE.sub("[已脱敏账号凭证]", out)
    out = _INLINE_ACCOUNT_PASS_RE.sub(r"\1[已脱敏账号]\3[已脱敏密码]", out)
    out = _INLINE_PASSWORD_RE.sub(lambda m: m.group(0) if "已脱敏" in m.group(2) else f"{m.group(1)}[已脱敏密码]", out)
    out = _IDCARD_18_RE.sub(lambda m: "[已脱敏身份证]" if _is_valid_cn_idcard(m.group(1)) else m.group(0), out)
    if re.search(r"2fa|验证器|totp|refresh\s*token|账号凭证", out, re.I):
        out = _TOTP_BASE32_32_RE.sub("[已脱敏2FA密钥]", out)
    for _, pat in SECRET_REDACT_RULES:
        out = pat.sub("[已脱敏凭证]", out)

    # Line-scoped contextual high-entropy catch (skip lines with code/URL markers)
    cleaned_lines = []
    for line in out.splitlines():
        if _CONTEXT_HINT_RE.search(line) and "http://" not in line and "https://" not in line and "/Users/" not in line:
            line = _CANDIDATE_TOKEN_RE.sub(
                lambda m: "[已脱敏高熵密钥]" if _is_high_entropy_secret(m.group(1)) else m.group(0),
                line,
            )
        cleaned_lines.append(line)
    res = "\n".join(cleaned_lines)
    if text.endswith("\n"):
        res += "\n"
    return res


def find_raw_secrets(text: str) -> list[str]:
    """Return matched credential rule labels if unredacted secrets remain in text."""
    hits: list[str] = []
    for label, pat in SECRET_REDACT_RULES:
        if pat.search(text):
            hits.append(label)
    if _AUTH_BEARER_RE.search(text):
        hits.append("Authorization Bearer 请求头")
    if any(_is_valid_cn_idcard(m.group(1)) for m in _IDCARD_18_RE.finditer(text)):
        hits.append("身份证号")
    for line in text.splitlines():
        if _CONTEXT_HINT_RE.search(line) and "http://" not in line and "https://" not in line and "/Users/" not in line:
            if any(_is_high_entropy_secret(m.group(1)) for m in _CANDIDATE_TOKEN_RE.finditer(line)):
                hits.append("高熵密钥串")
                break
    return list(dict.fromkeys(hits))


def load_noise_filter(root: Path) -> dict:
    cfg = dict(DEFAULT_NOISE_FILTER)
    cfg_path = root / "_同步" / "kb_config.json"
    if cfg_path.is_file():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8")).get("noise_filter")
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except Exception:
            pass
    return cfg


def clean_ide_prompt(text: str) -> str:
    """Strip IDE boilerplate headers (# Context from my IDE setup / # Files mentioned by the user / Referenced ChatGPT conversation)."""
    txt = (text or "").strip()
    if "## My request:" in txt:
        txt = txt.split("## My request:", 1)[1].strip()
    elif "User request:" in txt and "## Referenced ChatGPT conversation:" in txt:
        txt = txt.split("User request:", 1)[1].strip()
    elif txt.startswith(("# Files mentioned by the user:", "# Context from my IDE setup:", "## Referenced ChatGPT conversation:")):
        # If the entire string was truncated inside the header before '## My request:', return empty so caller falls back to body
        return ""
    return txt


def should_skip_conversation(
    conv_id: str,
    title: str,
    messages: list[dict],
    cfg: dict | None = None,
) -> bool:
    """Return True only for empty chats, synthetic *_OK connectivity probes, or pure greetings with zero work context."""
    nf = cfg or DEFAULT_NOISE_FILTER
    if conv_id in set(nf.get("excluded_ids") or []):
        return True
    if nf.get("drop_zero_message", True) and not messages:
        return True

    clean_title = clean_ide_prompt(title or "")
    excluded_topic_pat = nf.get("excluded_topic_regex")
    if excluded_topic_pat:
        opening_texts = [clean_title] + [
            clean_ide_prompt(m.get("text") or "")[:600]
            for m in messages[:4]
            if not m.get("context")
        ]
        if any(t and re.search(excluded_topic_pat, t, re.I) for t in opening_texts):
            return True

    probe_pat = nf.get("probe_title_regex")
    if probe_pat and len(messages) <= 6 and re.search(probe_pat, clean_title, re.I):
        return True

    if len(messages) > 5:
        return False

    user_msgs = [
        clean_ide_prompt(m.get("text") or "")
        for m in messages
        if m.get("role") == "user" and not m.get("context")
    ]
    user_msgs = [u for u in user_msgs if u]
    if not user_msgs:
        return True

    trivial_pat = nf.get("trivial_user_regex")
    if not trivial_pat or not all(re.match(trivial_pat, txt, re.I) for txt in user_msgs):
        return False

    assistant_msgs = [(m.get("text") or "").strip() for m in messages if m.get("role") == "assistant"]
    if not assistant_msgs:
        return True
    total_assistant_chars = sum(len(a) for a in assistant_msgs)
    has_concrete_ref = any(
        bool(re.search(r"/Users/|/opt/|\b[A-Z][a-z]+[A-Z][A-Za-z0-9]+\b", a))
        for a in assistant_msgs
    )
    return total_assistant_chars < 420 and not has_concrete_ref


def should_skip_exported_markdown(
    conv_id: str,
    title: str,
    md_text: str,
    cfg: dict | None = None,
) -> bool:
    """Apply should_skip_conversation to already-rendered Markdown when raw source logs are archived (.zst)."""
    messages: list[dict] = []
    cur_role: str | None = None
    buf: list[str] = []

    def _flush():
        if cur_role:
            txt = "\n".join(buf).strip()
            if txt:
                messages.append({"role": cur_role, "text": txt})

    for line in md_text.splitlines():
        m = re.match(r"^#{2,3}\s+\d+\.\s+(用户|助手)(.*)$", line)
        if m:
            _flush()
            cur_role = "user" if m.group(1) == "用户" else "assistant"
            buf = []
            continue
        if line.startswith(("时间（UTC）：", "<!-- message_id:")):
            continue
        if cur_role:
            buf.append(line)
    _flush()
    return should_skip_conversation(conv_id, title, messages, cfg)
