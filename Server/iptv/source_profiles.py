"""Isolated IPTV source profile settings; default American remains backward compatible."""
import hashlib
from pathlib import Path

PROFILES = ("american", "malay")

def profile_name(env):
    name = env.get("JEALOUSFIN_SOURCE_PROFILE", "american").strip().lower()
    if name not in PROFILES:
        raise ValueError("Unknown IPTV source profile")
    return name

def state_root(base, profile):
    if profile not in PROFILES:
        raise ValueError("Unknown IPTV source profile")
    root = Path(base).expanduser().resolve()
    if profile == "american":
        return root
    isolated = root / "profiles" / profile
    if isolated.resolve() != isolated:
        raise ValueError("Prototype state path must not be a symlink")
    return isolated

def gateway_port(env, profile):
    port = int(env.get("JEALOUSFIN_PORT", "18796" if profile == "american" else "18797"))
    if not 1 <= port <= 65535 or (profile == "malay" and port == 18796):
        raise ValueError("Invalid or production-reserved gateway port")
    return port

def minimum_channels(env, profile):
    if profile == "american":
        return 100
    # The pilot must have a reviewed expected size before it can start.
    raw = env.get("JEALOUSFIN_PROFILE_MIN_CHANNELS", "")
    if not raw.isdecimal() or not 1 <= int(raw) <= 9999:
        raise ValueError("Explicit reviewed prototype catalogue minimum required")
    return int(raw)

def channel_id(profile, provider_id):
    if profile == "american":
        # Existing IDs must remain byte-for-byte stable.
        return hashlib.md5(provider_id.encode()).hexdigest()
    if profile not in PROFILES:
        raise ValueError("Unknown IPTV source profile")
    return hashlib.sha256(("jealousfin:" + profile + "\0" + provider_id).encode()).hexdigest()[:32]
