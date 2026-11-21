"""插件授权判定（Batch32-2 派单侧）。

口径（三级判定，先后顺序写死）：
    1) revoked_instances 命中 → deny（一票否决，撤权最优先）
    2) granted_to 命中（实例级白名单）→ allow
    3) granted_roles 命中（岗位级兜底，岗位 = instance_id 的「-」前缀，如 ops-win → ops）→ allow
    4) 都没有 → deny

为什么这么定：B31 把权限边界定在实例上（--instance-id + 执行方口令），所以主判定必须是实例；
纯实例授权会让每台新机上线都要改清单，所以留岗位兜底；没有黑名单则「从岗位里摘一台机器」做不到。

额外两条：
- review_state 不是 reviewed 的插件一律 deny（draft / revoked 都不许派）；
- 带 plugin 的派单必须带 target_instance（不指定岗位就判不了授权，直接拒）。

索引文件路径：环境变量 MWP_PLUGINS_DIR 优先，缺省为仓库内 plugins/。
按 mtime+size 缓存解析结果，改完 index.json 不必重启平台。
"""
import json
import os
from typing import Any, Dict, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_cache: Dict[str, Any] = {"key": None, "data": None}


def plugins_root() -> str:
    return os.environ.get("MWP_PLUGINS_DIR") or os.path.join(ROOT, "plugins")


def instance_role(instance_id: str) -> str:
    """岗位 = instance_id 的「-」前缀（与看板方章同一口径）。没有前缀就整个当岗位。"""
    value = (instance_id or "").strip()
    return value.split("-", 1)[0] if "-" in value else value


def load_index() -> Tuple[Optional[Dict[str, Any]], str]:
    """返回 (index, error)。按 mtime+size 缓存。"""
    path = os.path.join(plugins_root(), "index.json")
    try:
        stat = os.stat(path)
    except OSError:
        return None, "插件索引不存在：%s" % path
    key = (path, stat.st_mtime_ns, stat.st_size)
    if _cache["key"] == key:
        return _cache["data"], ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return None, "插件索引解析失败：%s" % exc
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        return None, "插件索引结构不对（需要顶层 plugins 数组）"
    _cache["key"] = key
    _cache["data"] = data
    return data, ""


def _entry(index: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    for item in index.get("plugins", []):
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def judge_plugin(plugin_name: str, instance_id: str) -> Dict[str, Any]:
    """判定一个岗位能不能用这个插件。返回 {decision, reason, version, state}。"""
    index, err = load_index()
    if index is None:
        return {"decision": "deny", "reason": err, "version": None, "state": None}

    entry = _entry(index, plugin_name)
    if entry is None:
        return {"decision": "deny", "reason": "插件未登记（plugins/index.json 里没有 %s）" % plugin_name,
                "version": None, "state": None}

    version = str(entry.get("latest") or "")
    state = str(entry.get("review_state") or "")
    if state != "reviewed":
        return {"decision": "deny", "version": version, "state": state,
                "reason": "插件未过审核（review_state=%s），只允许 reviewed 的插件派单" % (state or "空")}

    instance = (instance_id or "").strip()
    if not instance:
        return {"decision": "deny", "version": version, "state": state,
                "reason": "派单带 plugin 时必须指定 target_instance（不指定岗位无法判授权）"}

    revoked = [str(x) for x in (entry.get("revoked_instances") or [])]
    granted = [str(x) for x in (entry.get("granted_to") or [])]
    roles = [str(x) for x in (entry.get("granted_roles") or [])]
    role = instance_role(instance)

    if instance in revoked:
        return {"decision": "deny", "version": version, "state": state,
                "reason": "实例在撤权名单（revoked_instances），撤权优先于一切授权"}
    if instance in granted:
        return {"decision": "allow", "version": version, "state": state,
                "reason": "实例命中 granted_to"}
    if role and role in roles:
        return {"decision": "allow", "version": version, "state": state,
                "reason": "岗位命中 granted_roles（%s）" % role}
    return {"decision": "deny", "version": version, "state": state,
            "reason": "实例 %s 与岗位 %s 均未授权（granted_to/granted_roles 都没有）"
                      % (instance, role or "-")}
