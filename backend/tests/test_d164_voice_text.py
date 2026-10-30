"""D164：语音播报内容要回答"哪台主机、哪块盘、使用率多少、超过阈值多少"。

用户验收要求（2026-10-22）："我想接收到的电话里面，要有的几个信息是
'哪台主机，哪个磁盘告警了超过阈值了' 哪台主机，哪块盘使用率是xxx，超过阈值xxx，
请立刻处理之类的——用户真实需要收到的语音内容。"

三个修复点：
  ① `_metric_info`：指标中文化（disk.percent→磁盘使用率）+ 业务维度进展示名
     （哪块盘=挂载点/设备、哪张网卡=interface、哪个实例=instance）；
  ② `_aliyun_voice_text`：剥 markdown（** 会被 TTS 念成"星号"）+ 触发时结尾
     "请尽快处理"（恢复不加）；
  ③ 磁盘按挂载点的明细指标念出盘符（disk.data.percent → "磁盘 data 分区使用率"）。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.notify_format import _metric_info, describe_alert
from app.services.notify_transport import _aliyun_voice_text


def _ev(**kw):
    base = dict(hash="abc123", rule_name="磁盘水位", metric="disk.percent", severity=1,
                trigger_value=87.6, tags={}, host_ident="d26f5e7a",
                first_trigger_time=None, trigger_time=None, is_recovered=False)
    base.update(kw)
    return SimpleNamespace(**base)


HOST = {"name": "demo-host-b", "ip": "203.0.113.12"}


def test_metric_info_chinese_and_dimensions():
    """指标中文化 + 哪块盘/哪张网卡/哪个实例。"""
    assert _metric_info(_ev(metric="disk.percent"))[0] == "磁盘使用率"
    assert _metric_info(_ev(metric="disk.data.percent"))[0] == "磁盘 data 分区使用率"
    label, _ = _metric_info(_ev(metric="disk.io.read.bytes", tags={"device": "vda"}))
    assert "磁盘读字节" in label and "vda" in label, label
    label, _ = _metric_info(_ev(metric="mem.percent"))
    assert label == "内存使用率"
    label, _ = _metric_info(_ev(metric="mysql.connections.current",
                                tags={"instance": "mysql-prod"}))
    assert "MySQL" in label and "mysql-prod" in label, label


def test_voice_text_has_all_four_facts():
    """电话文本四要素：主机名、盘、使用率、阈值 + 行动指令；不得含 markdown。"""
    ev = _ev(metric="disk.percent", trigger_value=87.6,
             tags={"mountpoint": "/", "device": "/dev/vda1"})
    title, body, _ = describe_alert(ev, {"name": "磁盘水位", "expr": ">85"}, HOST)
    vt = _aliyun_voice_text(title, body)
    assert "**" not in vt and "`" not in vt, "TTS 文本不得含 markdown 标记"
    assert "demo-host-b" in vt, "要有哪台主机"
    assert "磁盘" in vt, "要有哪块盘（中文）"
    assert "87.6" in vt, "要有使用率"
    assert "85" in vt, "要有阈值"
    assert "请尽快处理" in vt, "要有行动指令"
    # 截断上限内要保住"当前值/阈值"段（信息前置设计）
    assert "当前值" in vt[:_voice_cap()], f"200 字内应有当前值段：{vt}"


def _voice_cap():
    from app.services.notify_transport import _ALIYUN_VOICE_MAX_TEXT
    return _ALIYUN_VOICE_MAX_TEXT


def test_voice_recovered_no_action():
    """恢复播报不含"请尽快处理"。"""
    ev = _ev(is_recovered=True, metric="disk.percent", trigger_value=12.0)
    title, body, _ = describe_alert(ev, {"name": "磁盘水位", "expr": ">85"}, HOST)
    vt = _aliyun_voice_text(title, body)
    assert "请尽快处理" not in vt, vt


def test_voice_no_dims_still_reads_host_and_value():
    """Agent 标量指标（无维度标签）也要有主机名/使用率/阈值。"""
    ev = _ev(metric="mem.percent", trigger_value=91.2, tags={})
    title, body, _ = describe_alert(ev, {"name": "内存", "expr": ">90"}, HOST)
    vt = _aliyun_voice_text(title, body)
    assert "demo-host-b" in vt and "内存使用率" in vt
    assert "91.2" in vt and "90" in vt and "请尽快处理" in vt
