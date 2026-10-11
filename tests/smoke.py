#!/usr/bin/env python3
"""QQOps 冒烟 + 集成测试 —— 不依赖网络(除本机 loopback)、不触碰真实数据。

覆盖:
  * 配置(默认值/校验/掩码/原子写/持久化)
  * 存储(消息/群/运行历史/元数据/导出/备份/清理)
  * 全文检索(FTS5: 中文逐字、群名、重建索引、模式回退)
  * 总结器(CQ 清洗/统计块/截断/无 key 降级)
  * 采集器过滤(群/类型/私聊)
  * 旁路服务(NapCat/AstrBot 规格与开关)
  * Web 层(外壳/登录/会话/状态快照)
  * 一致性(版本号三处一致、纯标准库、无第三方依赖)
  * 集成(真实子进程: 采集器收消息落库 + SIGTERM 能退出; 控制台 HTTP/鉴权/缓存头)

运行: python3 tests/smoke.py -v
"""
import ast
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.environ.get("QQOPS_TEST_DIR") or os.path.join(ROOT, ".testdata")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["QQOPS_DATA"] = TMP
os.environ["TZ"] = "Asia/Shanghai"
sys.path.insert(0, ROOT)


def ci_note(text: str, title: str = "诊断") -> str:
    """把诊断信息打成 CI annotation + 返回给断言消息。

    为什么: GitHub 的 job 日志要登录才能下载, 而 `::error::` 生成的 annotation 是公开的;
    排查只在 CI 上出现的问题(跨平台/macOS/慢机器)时, 这是唯一可靠的"看到真相"通道。
    """
    text = str(text or "")
    if os.environ.get("GITHUB_ACTIONS"):
        for line in text.splitlines()[:25]:
            if line.strip():
                print("::error title=%s::%s" % (title, line.replace("%", "%25")), flush=True)
    return text

from qqops import (collector, core, extract, fileread, linkfetch, media, render,  # noqa: E402
                   summarizer, svc, tools, watchdog, webapp)
core.ensure_utf8_console()   # 非 UTF-8 控制台(如西文 Windows)下打印中文不能崩
from qqops import runner as runner_mod                                           # noqa: E402
from qqops.framework import app as fw_app                                        # noqa: E402
from qqops.framework import bus as fw_bus                                        # noqa: E402
from qqops.framework import errors as fw_errors                                  # noqa: E402
from qqops.framework import event as fw_event                                    # noqa: E402
from qqops.framework import llm as fw_llm                                        # noqa: E402
from qqops.framework import log as fw_log                                        # noqa: E402
from qqops.framework import pipeline as fw_pipeline                              # noqa: E402
from qqops.framework import registry as fw_registry                              # noqa: E402
from qqops.framework import schema as fw_schema                                  # noqa: E402
from qqops.framework import web as fw_web                                        # noqa: E402

POSIX = os.name != "nt"
PLUGIN_DIR = os.path.join(ROOT, "qqops", "plugins")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_port(port: int, timeout: float = 12.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket()
        s.settimeout(0.4)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except Exception:
            time.sleep(0.2)
        finally:
            s.close()
    return False


def owner_identifiable() -> bool:
    """本机能否认出"占端口的进程是不是自己人"。

    桌面 Linux: 读 /proc/net/tcp 反查 inode; Android: 该文件被禁, 只能用 pgrep + environ。
    Windows 两者都没有(测试里跳过依赖它的用例)。
    """
    return core.proc_net_ok() or bool(shutil.which("pgrep"))


def tiny_server() -> tuple:
    """占住一个随机端口的迷你 TCP 服务(真的 accept)。

    `port_open()` 是"连一下就断开"的探测: 监听方若不 accept, backlog 很快塞满,
    后续连接会被直接拒绝(Windows 上最明显), 于是"端口有人占着"会被误判成"端口空闲"。
    """
    import socketserver
    import threading

    class H(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                self.request.recv(64)
            except Exception:
                pass

    class Srv(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Srv(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    def stop():
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass

    return int(srv.server_address[1]), stop


class TestConfig(unittest.TestCase):
    def test_defaults_and_token(self):
        cfg = core.Config()
        self.assertTrue(cfg.data["web"]["token"])
        self.assertEqual(cfg.data["collector"]["port"], 8080)
        self.assertEqual(cfg.validate(), [])

    def test_persist_and_reload(self):
        cfg = core.Config()
        cfg.set_values({"collector": {"port": 8123}})
        self.assertEqual(core.Config().data["collector"]["port"], 8123)

    def test_mask_hides_secret(self):
        cfg = core.Config()
        cfg.set_values({"web": {"token": "abcdefghijkl"}})
        m = core.Config().masked()
        self.assertNotIn("abcdefghijkl", json.dumps(m))
        self.assertTrue(m["web"]["token"].endswith("ijkl"))

    def test_validation_catches_bad_port(self):
        cfg = core.Config()
        cfg.data["web"]["port"] = 99999
        self.assertTrue(any("web.port" in e for e in cfg.validate()))

    def test_set_values_ignores_masked_secret(self):
        cfg = core.Config()
        cfg.set_values({"web": {"token": "realtoken12345"}})
        cfg.set_values({"web": {"token": "****"}})
        self.assertEqual(core.Config().data["web"]["token"], "realtoken12345")

    def test_atomic_write_and_broken_config_recovery(self):
        cfg = core.Config()
        cfg.save()
        with open(core.CONF_PATH, "a", encoding="utf-8") as f:
            f.write("{ broken")
        recovered = core.Config()           # 损坏时不应抛异常, 且留有 .broken-* 备份
        self.assertTrue(recovered.data["web"]["port"])
        self.assertTrue(any(f.startswith("config.json.broken-") for f in os.listdir(core.DATA)))
        core.Config().save()


class TestStore(unittest.TestCase):
    _mid = [1000]        # 真实世界里 message_id 不会重复(3.1 起按它去重), 夹具也得讲道理

    def setUp(self):
        self.store = core.Store()

    @classmethod
    def next_mid(cls) -> int:
        cls._mid[0] += 1
        return cls._mid[0]

    def ev(self, gid=1001, text="hello", t=None, nick="tester", mid=None):
        return {"time": t or core.now(), "post_type": "message", "message_type": "group",
                "group_id": gid, "user_id": 2002, "message_id": mid or self.next_mid(),
                "raw_message": text, "sender": {"nickname": nick},
                "message": [{"type": "text", "data": {"text": text}}]}

    def test_insert_and_stats(self):
        self.assertTrue(self.store.insert_message(self.ev()))
        st = self.store.stats(24)
        self.assertGreaterEqual(st["total"], 1)
        self.assertEqual(len(st["series"]), 24)

    def test_filter_and_paging(self):
        for i in range(7):
            self.store.insert_message(self.ev(text="msg-%d" % i))
        res = self.store.messages(group="1001", hours=24, page=1, size=5)
        self.assertEqual(len(res["rows"]), 5)
        self.assertGreaterEqual(res["pages"], 2)

    def test_kinds_filter(self):
        self.store.insert_message({"time": core.now(), "message_type": "group", "group_id": 1,
                                   "user_id": 1, "raw_message": "", "sender": {},
                                   "message": [{"type": "image", "data": {}}]})
        res = self.store.messages(kind="image", hours=24)
        self.assertTrue(all("image" in r["kinds"] for r in res["rows"]))

    def test_groups_toggle_and_enabled(self):
        self.store.upsert_group(555, "测试群")
        self.store.upsert_group(555, "", 1)
        self.assertIn(555, self.store.enabled_groups())
        self.assertEqual(self.store.group_rows()[555]["name"], "测试群")

    def test_run_history(self):
        rid = self.store.run_start("test")
        self.store.run_finish(rid, "ok", "detail", "report.md", 10, 20, 0.01)
        r = self.store.runs(1)[0]
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["tokens_in"], 10)

    def test_export_csv_has_bom_for_excel(self):
        self.store.insert_message(self.ev())
        data = self.store.export_csv(24)
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"group_id", data)

    def test_backup_and_prune(self):
        self.store.insert_message(self.ev(t=core.now() - 90 * 86400))
        p = self.store.backup(core.BACKUP_DIR)
        self.assertTrue(os.path.isfile(p))
        self.assertGreaterEqual(self.store.prune(30), 1)

    def test_meta_roundtrip(self):
        self.store.meta_set("k", "v")
        self.assertEqual(self.store.meta_get("k"), "v")


class TestFullText(unittest.TestCase):
    """中文全文检索: unicode61 会把整串中文当一个词, 必须逐字索引 + 短语查询。

    注意: 各用例共用同一个库, 因此查询一律用 group= 限定, 避免相互干扰。
    """

    def setUp(self):
        self.store = core.Store()
        self.gid = 9000 + int(time.time() * 1000) % 1000     # 每例独占一个群
        self.store.upsert_group(self.gid, "测试群Alpha")

    def put(self, text, nick="甲"):
        self.store.insert_message({"time": core.now(), "message_type": "group", "group_id": self.gid,
                                   "user_id": 1, "raw_message": text, "sender": {"nickname": nick},
                                   "message": [{"type": "text", "data": {}}]})

    def find(self, q):
        return self.store.messages(group=str(self.gid), q=q, hours=24)

    def test_fts_text_splits_cjk(self):
        self.assertEqual(core.fts_text("头像"), "头 像")
        self.assertEqual(core.fts_text("hello世界"), "hello 世 界")

    def test_fts_query_is_safe(self):
        q = core.fts_query('头像 AND "x"')
        self.assertIn('"头 像"', q)
        self.assertIn('"AND"*', q)        # 关键字被引号包裹, 不会当语法解释
        for bad in ['"', "*", "NEAR(", "a:b", "-x", "OR", "()"]:
            core.fts_query(bad)          # 不应抛异常, 也不应原样透传语法
        self.assertEqual(core.fts_query(""), "")

    def test_cjk_substring_search(self):
        self.put("吧唧头像裁剪讨论")
        self.assertEqual(self.find("头像")["total"], 1)
        self.assertEqual(self.find("头像")["mode"], "fts")
        self.assertEqual(self.find("吧唧头像")["total"], 1)
        self.assertEqual(self.find("裁剪")["total"], 1)

    def test_latin_prefix_search(self):
        self.put("deepseek is cheap", nick="bob")
        self.assertEqual(self.find("deep")["total"], 1)
        self.assertEqual(self.find("bob")["total"], 1)

    def test_group_name_search_and_rename(self):
        self.put("abc")
        self.assertEqual(self.find("Alpha")["total"], 1)
        self.store.upsert_group(self.gid, "改名后的群Beta")      # 改名后索引副本要跟着更新
        self.assertEqual(self.find("Alpha")["total"], 0)
        self.assertEqual(self.find("Beta")["total"], 1)

    def test_rebuild_fts(self):
        self.put("重建索引测试内容")
        self.assertGreaterEqual(self.store.rebuild_fts(), 1)
        self.assertEqual(self.find("索引")["total"], 1)
        self.assertGreaterEqual(self.store.fts_count(), 1)

    def test_no_match_returns_empty(self):
        self.put("普通内容")
        self.assertEqual(self.find("绝不会出现的词xyz")["total"], 0)


class TestSummarizer(unittest.TestCase):
    def test_clean_strips_cq_payload(self):
        raw = "[CQ:image,summary=&#91;图片&#93;,file=abc.jpg,url=https://x/y]"
        out = summarizer.clean(raw)
        self.assertEqual(out, "[图片]")
        self.assertNotIn("url=", out)

    def test_clean_json_card(self):
        self.assertEqual(summarizer.clean('[CQ:json,data={"a":1}]'), "[卡片]")

    def test_stats_block(self):
        rows = [{"nickname": "a", "kinds": "text", "ts": core.now()},
                {"nickname": "a", "kinds": "text,image", "ts": core.now()},
                {"nickname": "b", "kinds": "text", "ts": core.now()}]
        s = summarizer.stats_block(rows)
        self.assertIn("消息总数: **3**", s)
        self.assertIn("a(2)", s)

    def test_transcript_truncates(self):
        rows = [{"nickname": "n%d" % i, "kinds": "text", "ts": core.now(),
                 "raw_message": "x" * 100} for i in range(50)]
        t = summarizer.transcript_of(rows, 500)
        self.assertLessEqual(len(t), 560)
        self.assertTrue(t.startswith("(较早内容已省略)"))

    def test_run_without_key_outputs_stats_only(self):
        cfg = core.Config()
        cfg.set_values({"summarizer": {"api_key": "", "groups": [777], "min_messages": 1}})
        st = core.Store()
        st.insert_message({"time": core.now(), "message_type": "group", "group_id": 777,
                           "user_id": 1, "raw_message": "hi", "sender": {"nickname": "u"},
                           "message": [{"type": "text", "data": {}}]})
        res = summarizer.run(hours=24, groups=[777], no_push=True)
        self.assertIn(res.get("status"), ("ok", "partial", "empty"))
        if res.get("report"):
            self.assertTrue(os.path.isfile(res["report"]))

    def test_cost_calculation(self):
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}
        self.assertAlmostEqual(summarizer.cost_of(usage, {"input_price_per_mtok": 1.0,
                                                          "output_price_per_mtok": 2.0}), 2.0)


class TestCollectorFilter(unittest.TestCase):
    def _h(self, patch=None):
        cfg = core.Config()
        if patch:
            cfg.set_values(patch)
        h = object.__new__(collector.Handler)
        h.cfg = cfg
        return h

    def test_group_and_kind_filters(self):
        h = self._h({"collector": {"groups": [1], "kinds": ["text"], "store_private": False}})
        self.assertTrue(h._keep({"message_type": "group", "group_id": 1,
                                 "message": [{"type": "text", "data": {}}]}))
        self.assertFalse(h._keep({"message_type": "group", "group_id": 2,
                                  "message": [{"type": "text", "data": {}}]}))
        self.assertFalse(h._keep({"message_type": "group", "group_id": 1,
                                  "message": [{"type": "image", "data": {}}]}))
        self.assertFalse(h._keep({"message_type": "private", "user_id": 9, "message": "x"}))

    def test_empty_filters_accept_all(self):
        h = self._h()
        self.assertTrue(h._keep({"message_type": "group", "group_id": 42, "message": "x"}))


class TestExtras(unittest.TestCase):
    def test_specs_present(self):
        for name in ("napcat", "astrbot"):
            spec = core._extra_spec(core.Config(), name)
            self.assertTrue(spec["pattern"] and spec["start"])

    def test_kill_pattern_does_not_kill_caller(self):
        """回归: `qqops astrbot stop` 的命令行里含 "astrbot", 直接 pkill 会把调用方自己杀掉。

        这里让一个子进程在"自己的命令行含关键字"的情况下调用 kill_pattern,
        它必须能正常活下来并把结果打印出来。
        """
        if not POSIX or not shutil.which("pgrep"):
            self.skipTest("需要 pgrep/POSIX")
        pat = "qqops-selftest-%d" % os.getpid()
        # 诱饵必须用不会被 shell exec 优化掉 argv 的方式启动
        # (bash -c "sleep 20 # pat" 会被 bash 直接 exec 成 sleep, 关键字就没了)
        decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)", pat],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            code = (
                "import sys; sys.path.insert(0, %r)\n"
                "from qqops import core\n"
                "n = core.kill_pattern(%r, sig=0)\n"      # sig=0: 只探测不真杀
                "print('SURVIVED', n)\n" % (ROOT, pat)
            )
            p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            self.assertEqual(p.returncode, 0,
                             "调用方被自己杀掉了(回归): rc=%s out=%s" % (p.returncode, p.stdout))
            self.assertIn("SURVIVED", p.stdout)
            self.assertGreaterEqual(int(p.stdout.split()[1]), 1, "应至少匹配到诱饵进程")
        finally:
            decoy.kill()

    def test_ancestor_pids_contains_parent(self):
        if not POSIX:
            self.skipTest("仅 Linux")
        self.assertIn(os.getppid(), core._ancestor_pids())

    def test_status_shape(self):
        st = core.extra_status(core.Config())
        self.assertEqual(set(st), {"napcat", "astrbot"})
        for v in st.values():
            self.assertIn("running", v)
            self.assertIn("label", v)

    def test_unknown_extra_is_rejected(self):
        self.assertIn("未知旁路服务", core.extra_action(core.Config(), "nonexistent", "start"))
        self.assertEqual(core._extra_spec(core.Config(), "nonexistent"), {})

    def test_config_override_applies(self):
        cfg = core.Config()
        try:
            cfg.set_values({"extras": {"astrbot": {"pattern": "[m]y-astrbot"}}})
            self.assertEqual(core._extra_spec(core.Config(), "astrbot")["pattern"], "[m]y-astrbot")
        finally:
            cfg.set_values({"extras": {"astrbot": {"pattern": core.EXTRAS["astrbot"]["pattern"]}}})


class TestWebAndNapcat(unittest.TestCase):
    def test_shell_renders(self):
        html = webapp.SHELL % {"ver": core.VERSION, "assets": webapp.ASSET_VER, "refresh": 15}
        self.assertIn("QQOps", html)
        self.assertIn("app.js", html)
        self.assertIn("status-strip", html)

    def test_login_page_renders(self):
        html = webapp.LOGIN % {"ver": core.VERSION, "assets": webapp.ASSET_VER, "err": ""}
        self.assertIn("访问令牌", html)

    def test_session_lifecycle(self):
        tk = webapp._new_session(1)
        self.assertTrue(webapp._session_ok(tk))
        webapp.SESSIONS[tk] = core.now() - 10
        self.assertFalse(webapp._session_ok(tk))

    def test_asset_fingerprint_changes_with_content(self):
        self.assertTrue(webapp.ASSET_VER.startswith(core.VERSION))
        self.assertGreater(len(webapp.ASSET_VER.split(".")[-1]), 6)

    def test_status_snapshot_shape(self):
        snap = core.status_snapshot(core.Config())
        for key in ("services", "extras", "ecosystem", "scheduler", "messages", "storage", "fts"):
            self.assertIn(key, snap)
        self.assertIn("next", snap["scheduler"])
        self.assertIn("collector", snap["services"])

    def test_napcat_card_and_forward_payloads(self):
        """卡片/转发/签名卡片的载荷必须符合 OneBot v11 结构, 且失败要能退回文本。"""
        nc = core.NapCat(port=3000, token="t")
        sent = []

        def fake_call(action, payload=None, timeout=None):
            sent.append((action, payload or {}))
            return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        nc.call = fake_call
        # 1) 合并转发: 节点结构 + 走 send_private_forward_msg
        node = core.forward_node("QQOps", 10001, "正文", title="【标题】")
        self.assertEqual(node["type"], "node")
        self.assertEqual(node["data"]["uin"], "10001")
        self.assertIn("【标题】", node["data"]["content"][0]["data"]["text"])
        self.assertEqual(nc.send_forward([node], user_id=777).get("retcode"), 0)
        self.assertEqual(sent[-1][0], "send_private_forward_msg")
        self.assertTrue(sent[-1][1]["messages"])
        # 2) 裸 JSON 卡片: 成功时只发一条卡片; 失败时退回纯文本
        sent.clear()
        self.assertEqual(nc.send_card(core.qq_card("t", "d"), user_id=777).get("retcode"), 0)
        self.assertEqual([a for a, _ in sent], ["send_private_msg"])
        self.assertIn('"type": "json"', json.dumps(sent[0][1], ensure_ascii=False))
        sent.clear()

        def fail_call(action, payload=None, timeout=None):
            sent.append((action, payload or {}))
            return {"status": "failed", "retcode": 1200, "message": "卡片版本过低"}

        nc.call = fail_call
        nc.send_card(core.qq_card("t", "d"), user_id=777, fallback_text="兜底文本")
        self.assertEqual([a for a, _ in sent], ["send_private_msg", "send_private_msg"])
        self.assertIn("兜底文本", json.dumps(sent[1][1], ensure_ascii=False))
        nc.call = fake_call
        # 3) 签名卡片: 拿不到签名 → 退回文本, 并把原因说出来
        sent.clear()
        nc.call = lambda action, payload=None, timeout=None: (
            {"status": "failed", "retcode": 400, "message": "packetBackend不可用"}
            if action == "get_mini_app_ark" else
            sent.append((action, payload)) or {"status": "ok", "retcode": 0})
        res = nc.send_miniapp_card("标题", "摘要", user_id=777, fallback_text="退文本")
        self.assertEqual(res.get("retcode"), 0, res)
        self.assertEqual(sent[-1][0], "send_private_msg")
        ark, err = nc.miniapp_ark("标题", "摘要")
        self.assertEqual(ark, "")
        self.assertIn("packetBackend", err)
        # 4) Markdown 卡片: 段结构对, 失败(内核 timeout/不支持)必须退文本
        sent.clear()
        nc.call = fake_call
        self.assertEqual(nc.send_markdown("## 标题\n\n- 项", user_id=777).get("retcode"), 0)
        seg = json.dumps(sent[-1][1], ensure_ascii=False)
        self.assertIn('"type": "markdown"', seg)
        self.assertIn('"content"', seg)
        sent.clear()
        nc.call = fail_call
        nc.send_markdown("## 标题", user_id=777, fallback_text="退文本")
        self.assertEqual([a for a, _ in sent], ["send_private_msg", "send_private_msg"])
        self.assertIn("退文本", json.dumps(sent[-1][1], ensure_ascii=False))
        # 5) 官方分享卡片: 从 arkMsg 取出即可发
        nc.call = lambda action, payload=None, timeout=None: (
            {"status": "ok", "retcode": 0,
             "data": {"errCode": 0, "arkMsg": '{"app":"com.tencent.contact.lua"}'}}
            if action == "ArkSharePeer" else {"status": "ok", "retcode": 0})
        ark2, err2 = nc.ark_share_peer(user_id=777)
        self.assertIn("contact.lua", ark2)
        self.assertEqual(err2, "")
        # 6) CQ 码转人话(聊天里提问要先把原始消息洗干净)
        self.assertEqual(core.cq_plain("[CQ:image,file=a.jpg] 问 谁交报告 [CQ:at,qq=1]"),
                         "[图片] 问 谁交报告")

    def test_napcat_client_builds(self):
        nc = core.NapCat(port=3000, token="t")
        self.assertEqual(nc.base, "http://127.0.0.1:3000")
        self.assertEqual(core.human_bytes(1536), "1.5 KB")
        self.assertEqual(core.kinds_of([{"type": "image"}, {"type": "record"}]), "image,voice")

    def test_next_run_time(self):
        nxt = core.next_run_time(["23:59"])
        self.assertGreater(nxt["ts"], core.now())
        self.assertLessEqual(nxt["in"], 86400)

    def test_svc_module_exposes_actions(self):
        self.assertEqual(set(core.SERVICE_MODULES),
                         {"collector", "scheduler", "web", "watchdog"})
        self.assertEqual(set(core.WATCH_TARGETS), {"collector", "scheduler", "web"})
        self.assertTrue(callable(svc.main))


class TestLifecycle(unittest.TestCase):
    """启动/停止的可靠性: 秒退必须报失败, PID 校验不能杀错进程, 看门狗体检无副作用。"""

    def test_health_rejects_unknown_and_dead(self):
        self.assertFalse(core.service_health("nosuch")["ok"])
        core.SERVICE_MODULES["zzz"] = "qqops.nothere"
        try:
            r = core.service_health("zzz", timeout=1)
            self.assertFalse(r["ok"])
            self.assertIn("进程不存在", r["why"])
        finally:
            core.SERVICE_MODULES.pop("zzz", None)

    def test_start_service_reports_fast_exit(self):
        """模块不存在时进程秒退 —— 必须报"启动失败"而不是"已启动"。"""
        if not POSIX:
            self.skipTest("依赖 /proc 与 pgrep")
        orig = core.SERVICE_MODULES.get("web")
        core.SERVICE_MODULES["web"] = "qqops.definitely_not_a_module"
        try:
            msg = core.start_service("web", wait=8)
            self.assertIn("启动失败", msg, msg)
            self.assertNotIn("已启动", msg)
        finally:
            if orig:
                core.SERVICE_MODULES["web"] = orig

    def test_pid_validation(self):
        if not POSIX or not os.path.isdir("/proc"):
            self.skipTest("需要 /proc")
        self.assertFalse(core._pid_alive_and_matches(os.getpid(), "qqops.nonexistent"))
        self.assertTrue(core._pid_alive_and_matches(os.getpid(), "python"))
        self.assertFalse(core._pid_alive_and_matches(999999, "python"))

    def test_watchdog_check_once_shape(self):
        res = watchdog.check_once(core.Config(), auto_start=False)
        self.assertGreaterEqual(len(res), 3)
        for name, state, msg in res:
            self.assertIn(name, core.WATCH_TARGETS)
            self.assertIn(state, ("ok", "unhealthy", "dead"))
            self.assertTrue(msg)

    def test_watchdog_once_cli(self):
        p = subprocess.run([sys.executable, "-m", "qqops.watchdog", "--once"], cwd=ROOT,
                           env=self._env(), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("collector", p.stdout or "")

    def test_exit_code_soon(self):
        """秒退的进程必须能被确认退出码(否则手机上会误报成"启动异常")。"""
        p = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
        self.assertEqual(core._exit_code_soon(p, 8.0), 3)
        q = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertIsNone(core._exit_code_soon(q, 0.4))
        finally:
            q.kill()
            q.wait(timeout=10)

    def test_svc_port_lookup(self):
        cfg = core.Config()
        self.assertEqual(core._svc_port("collector"), int(cfg.data["collector"]["port"]))
        self.assertEqual(core._svc_port("web"), int(cfg.data["web"]["port"]))
        self.assertEqual(core._svc_port("scheduler"), 0)

    def test_port_conflict_detected(self):
        """端口被占用必须能被发现; 能认出占用者是不是自己人。

        手机上"点启动→显示已启动→其实没起来"的根因就是上一份僵死进程还占着端口。
        """
        port, stop = tiny_server()
        try:
            self.assertTrue(core.port_open(port))
            conf = core.check_port_free(port, "qqops.collector")
            self.assertFalse(conf["ok"])
            self.assertIn("占用", conf["why"])
            self.assertIn(str(port), conf["why"])
            self.assertFalse(conf["ours"])          # 占用者是测试进程, 不是 collector
            if conf.get("pid"):
                # 能定位到占用者(桌面 Linux 走 /proc/net/tcp, macOS 走 lsof)→ 必须认出是测试进程
                self.assertEqual(conf["pid"], os.getpid(), conf)
                # 模块名命中自身命令行 → 认得出"自己人"(仅在本机安装记录/环境变量口径一致时)
                self.assertTrue(core.check_port_free(port, "smoke")["ours"])
            else:
                # Android: /proc/net/tcp 被禁、也没有 lsof, 只能如实说"定位不到", 不能瞎猜
                self.assertIsNone(conf["pid"])
                self.assertIn("无法定位", conf["why"])
        finally:
            stop()
        self.assertFalse(core.check_port_free(port, "qqops.collector"))

    def test_reclaim_refuses_foreign_port(self):
        """端口被别的程序占着 → 必须明确拒绝, 而不是拉起一个必然秒退的进程。"""
        port, stop = tiny_server()
        try:
            bad, note = core._reclaim_port("collector", port, "qqops.collector")
            self.assertTrue(bad, "应拒绝: %r" % (bad,))
            self.assertIn("占用", bad)
            self.assertEqual(note, "")
            self.assertTrue(core.port_open(port), "外部占用者不该被杀掉")
        finally:
            stop()

    def _spawn_listener(self, port: int, tag: str, data_dir: str = None, pid_file: str = ""):
        """起一个真的在 accept 的子进程占住端口(tag 会出现在它的命令行里)。

        * 必须真的 accept, 否则 backlog 塞满后连不上, 探测会误判成"端口已释放";
        * 命令行要带上 `-m <tag>` —— `_own_pids()` 正是按这个形状找自己人的,
          真实服务就是 `python -m qqops.collector` 这种命令行。
        """
        code = ("import socket\n"
                "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
                "s.bind(('127.0.0.1',%d));s.listen(8);s.settimeout(0.5)\n"
                "print('up',flush=True)\n"
                "while True:\n"
                "    try:\n"
                "        c,_=s.accept();c.close()\n"
                "    except Exception:\n"
                "        pass\n" % port)
        env = dict(os.environ)
        if data_dir is not None:
            env["QQOPS_DATA"] = data_dir
        p = subprocess.Popen([sys.executable, "-c", code, "-m", tag], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.assertEqual(p.stdout.readline().strip(), b"up", "子进程没能占住端口")
        if pid_file:                      # 真实服务会写 data/run/<服务>.pid, 这里照做
            core.ensure_dirs()
            with open(pid_file, "w", encoding="utf-8") as f:
                f.write(str(p.pid))
        return p

    def test_reclaim_kills_own_zombie(self):
        """占用者是我们自己的僵死进程 → 自动回收并给出提示。"""
        if not owner_identifiable():
            self.skipTest("本机既读不了 /proc/net/tcp 也没有 pgrep, 无法认定自己人")
        port = free_port()
        fake = "qqops.fakecollector"
        pidf = core.pid_path("collector")
        p = self._spawn_listener(port, fake, core.DATA, pid_file=pidf)
        try:
            self.assertTrue(core.port_open(port))
            conf = core.check_port_free(port, fake)
            self.assertTrue(conf["ours"], conf)
            self.assertEqual(conf["pid"], p.pid)
            bad, note = core._reclaim_port("collector", port, fake)
            self.assertEqual(bad, "")
            self.assertIn("已回收", note)
            self.assertIn(str(p.pid), note)
            self.assertFalse(core.port_open(port))
        finally:
            p.kill()
            p.wait(timeout=10)

    def test_port_owner_needs_same_data_dir(self):
        """同模块但 QQOPS_DATA 不同 = 另一套实例, 绝不能当僵死进程回收。

        (在真机上跑测试时, 这正是"测试把生产实例干掉"的防线。)
        """
        if not owner_identifiable():
            self.skipTest("本机既读不了 /proc/net/tcp 也没有 pgrep")
        fake = "qqops.fakecollector"
        port = free_port()
        p = self._spawn_listener(port, fake, os.path.join(TMP, "other-install-data"))
        try:
            conf = core.check_port_free(port, fake)
            self.assertFalse(conf["ours"], conf)
            bad, note = core._reclaim_port("collector", port, fake)
            self.assertTrue(bad, "不该回收另一套实例")
            self.assertEqual(note, "")
            self.assertTrue(core.port_open(port), "那个实例必须还活着")
        finally:
            p.kill()
            p.wait(timeout=10)
        port2 = free_port()
        # 这一半假装"本机安装记录里的服务进程": 环境变量一致 + pid 文件指向它
        p2 = self._spawn_listener(port2, fake, core.DATA,
                                  pid_file=core.pid_path("collector"))
        try:
            self.assertTrue(core.check_port_free(port2, fake)["ours"])
        finally:
            p2.kill()
            p2.wait(timeout=10)

    def test_paths_independent_of_cwd_and_home(self):
        """数据目录必须由代码位置决定, 不能靠 $HOME 猜。

        (真踩过: 从别的目录/别的 HOME 调用 CLI 时, 程序在别处另建了一套空数据, 控制台显示"消息 0 条"。)
        """
        env = {k: v for k, v in os.environ.items()
               if k not in ("QQOPS_DATA", "QQOPS_HOME")}
        code = ("import os, sys; sys.path.insert(0, %r);"
                "from qqops import core; print(core.DATA)" % ROOT)
        for cwd, home in (("C:\\" if not POSIX else "/", "/tmp/nonexistent-home"),
                          (ROOT, "/tmp/another-home")):
            e = dict(env)
            e["HOME"] = home
            try:
                e["USERPROFILE"] = home
            except Exception:
                pass
            p = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=e,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60)
            got = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
            self.assertEqual(os.path.realpath(got), os.path.realpath(os.path.join(ROOT, "data")),
                             "cwd=%s HOME=%s 时数据目录跑偏了: %r %s" % (cwd, home, got, p.stderr[-200:]))

    def test_store_integrity_and_wal(self):
        """数据库应处于 WAL 模式并能通过完整性自检(手机断电/被杀后要能发现损坏)。"""
        st = core.Store()
        ok, detail = st.check_integrity()
        self.assertTrue(ok, detail)
        con = st.conn()
        try:
            mode = con.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(str(mode).lower(), "wal", mode)
            self.assertGreater(int(con.execute("PRAGMA busy_timeout").fetchone()[0]), 0)
        finally:
            con.close()

    def test_legacy_pidfiles_cleaned(self):
        """旧版本遗留的 run/qqops.<模块>.pid 必须被清掉, 且不碰现行 <服务>.pid。"""
        core.ensure_dirs()
        legacy = os.path.join(core.RUN_DIR, "qqops.collector.pid")
        cur = os.path.join(core.RUN_DIR, "collector.pid")
        with open(legacy, "w") as f:
            f.write("9245")
        with open(cur, "w") as f:
            f.write(str(os.getpid()))
        try:
            core.ensure_dirs()
            self.assertFalse(os.path.exists(legacy), "遗留 PID 文件没清掉")
            self.assertTrue(os.path.exists(cur), "现行 PID 文件被误删")
        finally:
            for p in (legacy, cur):
                try:
                    os.remove(p)
                except Exception:
                    pass

    def test_watchdog_healthy_right_after_start(self):
        """看门狗刚启动就必须是可判定的健康状态(先落心跳再体检)。

        手机上真踩过: 首轮体检要好几秒, 期间 service_health 判成"没心跳" →
        `qqops start` 报出假的"watchdog 启动异常"。
        """
        if not POSIX:
            self.skipTest("依赖 pgrep 与 /proc")
        cfg = core.Config()
        cfg.set_values({"watchdog": {"enabled": False, "interval": 15}})   # 只测心跳, 不拉起服务
        core.beat("watchdog", 0)                 # 心跳归零(心跳现在是独立文件)
        p = subprocess.Popen([sys.executable, "-m", "qqops.watchdog"], cwd=ROOT,
                             env=self._env(), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        try:
            h = core.service_health("watchdog", timeout=20)
            self.assertTrue(h["ok"], h)
            self.assertGreater(int(core.last_beat("watchdog") or 0), 0)
        finally:
            p.kill()
            p.wait(timeout=10)

    def _env(self):
        env = dict(os.environ)
        env["QQOPS_DATA"] = TMP
        env["TZ"] = "Asia/Shanghai"
        env["PYTHONIOENCODING"] = "utf-8"      # 子进程打印中文不能崩(西文 Windows)
        env["QQOPS_DEBUG_START"] = "7"         # 启动卡住就把线程栈打出来(CI 只能靠它看真相)
        return env


class TestPackaging(unittest.TestCase):
    """打包器: 清单完整, 且权限位必须显式写入(Windows 上打包不能丢执行位)。"""

    def _packer(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("qqops_pack",
                                                      os.path.join(ROOT, "pack.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_mode_for_executables(self):
        m = self._packer()
        for rel in ("bin/qqops", "install.sh", "uninstall.sh", "pack.sh",
                    "tools/android-keepalive.sh"):
            self.assertEqual(m.mode_for(rel), 0o755, rel)
        for rel in ("qqops/core.py", "VERSION", "config.example.json", "README.md"):
            self.assertEqual(m.mode_for(rel), 0o644, rel)

    def test_package_contains_no_private_files(self):
        """发布包不得含本机禁词表等私有文件(真机被抓到过一次: tools/ 整目录打包带上了禁词表)。

        这是**发布路径**的红线检查: 仓库干净不代表打包干净 —— 文件是按目录整批加的。
        """
        sys.path.insert(0, ROOT)
        import pack as pack_mod                                      # noqa: E402
        out_dir = os.path.join(TMP, "packcheck")
        path = pack_mod.build(out_dir, verbose=False, scan=False)
        try:
            bad = pack_mod.verify_no_private(path)
            self.assertEqual(bad, [], "发布包里有隐私: %s" % bad)
            with tarfile.open(path) as tf:
                names = [n.split("/", 1)[-1] for n in tf.getnames() if "/" in n]
            for rel in names:                                        # 私有文件名一律不许出现
                base = os.path.basename(rel)
                self.assertNotIn(base, pack_mod.SKIP_FILES, "发布了私有文件: %s" % rel)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def test_items_all_exist(self):
        m = self._packer()
        for item in m.ITEMS:
            self.assertTrue(os.path.exists(os.path.join(ROOT, item)), "包里缺 %s" % item)
        self.assertIn("tests", m.ITEMS)          # 手机上也要能自测
        self.assertNotIn("data", m.ITEMS)        # 数据不能被包进去

    def test_tarball_roundtrip_keeps_modes(self):
        """真打一次包再读回来: 执行位必须在包里(不再依赖解包后 chmod)。"""
        m = self._packer()
        out_dir = os.path.join(TMP, "packout")       # 不用 mkdtemp: 本机沙箱会拒绝
        try:
            path = m.build(out_dir, verbose=False, scan=False)
            with tarfile.open(path) as tf:
                modes = {ti.name.split("/", 1)[1]: ti.mode for ti in tf.getmembers()
                         if "/" in ti.name and ti.isfile()}
            self.assertEqual(modes.get("bin/qqops"), 0o755, modes.get("bin/qqops"))
            self.assertEqual(modes.get("qqops/core.py"), 0o644)
            self.assertIn("tests/smoke.py", modes)
            self.assertNotIn("data/config.json", modes)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


class TestConsistency(unittest.TestCase):
    def test_version_matches_everywhere(self):
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), core.VERSION)
        import qqops
        self.assertEqual(qqops.__version__, core.VERSION)
        self.assertEqual(core.DEFAULT_CONFIG["version"], core.VERSION)

    def test_stdlib_only(self):
        """核心必须只依赖标准库; 允许的可选依赖只能是函数内惰性导入(PDF 解析)。"""
        std = set(sys.stdlib_module_names)
        OPTIONAL = {"pypdf", "PyPDF2"}
        outside, lazy_ok = set(), set()
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "qqops")):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                src = open(path, encoding="utf-8").read()
                tree = ast.parse(src)
                top_level = set()
                for node in tree.body:                      # 模块级 import
                    if isinstance(node, ast.Import):
                        top_level.update(a.name.split(".")[0] for a in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        top_level.add(node.module.split(".")[0])
                for node in ast.walk(tree):                 # 所有 import(含函数内)
                    if isinstance(node, ast.Import):
                        outside.update(a.name.split(".")[0] for a in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        outside.add(node.module.split(".")[0])
                for m in (top_level & OPTIONAL):
                    lazy_ok.add(m)                          # 出现在模块级 → 记为违规
        bad = sorted(m for m in outside if m not in std and m != "qqops" and m not in OPTIONAL)
        self.assertEqual(bad, [], "出现第三方依赖: %s" % bad)
        self.assertEqual(sorted(lazy_ok), [], "可选依赖必须函数内惰性导入, 不能在模块级: %s" % lazy_ok)

    def test_no_debug_leftovers(self):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "qqops")):
            for fn in files:
                if fn.endswith(".py") or fn.endswith(".js"):
                    src = open(os.path.join(dirpath, fn), encoding="utf-8").read()
                    self.assertNotIn("DSH DEBUG", src, fn)
                    self.assertNotIn("breakpoint()", src, fn)
                    self.assertNotIn("pdb.set_trace", src, fn)


    def test_repo_has_no_secrets_or_personal_data(self):
        """仓库里不得出现隐私/密钥 —— 这是硬性发布门禁(工具: tools/check_secrets.py)。

        为什么写进测试而不是只靠 review: 这个项目天然贴着 QQ 号/群号/令牌/设备号,
        人肉看迟早漏。这里跑一遍扫描器, 保证"任何一次提交都不可能把隐私带进仓库"。
        """
        if not os.path.isdir(os.path.join(ROOT, ".git")):
            self.skipTest("不是 git 检出(部署安装): 仓库级隐私门禁在 CI/开发机上跑")
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import check_secrets as cs                                   # noqa: E402

        # 1) 通用规则 + 本机禁词表: 当前跟踪文件必须干净
        deny = cs.load_denylist()
        files = cs.iter_files(None, cs.load_ignore())
        findings = [f for f in cs.scan_files(files, deny) if f[0] == "hard"]
        self.assertEqual(findings, [], "发现隐私/密钥: %s" % findings[:5])
        self.assertGreater(len(files), 30, "扫描文件数异常, 扫描器可能没在工作")

        # 2) 扫描器本身要真的能抓到东西(否则"通过"毫无意义)。
        #    注意: 下面的假密钥/假路径**故意用拼接构造**而不是字面量 —— 否则扫描器会(正确地)
        #    把测试文件自己当成泄漏。仓库里因此不需要任何豁免。
        fake_key = "sk-" + "a" * 20 + "b" * 12
        bad = cs.scan_text("x.py", 'API_KEY = "%s"\n' % fake_key, [])
        self.assertTrue([f for f in bad if f[1] == "openai-key"], bad)
        fake_path = "C:" + "\\Users\\" + "somebody" + "\\qqops\\data"
        bad = cs.scan_text("x.md", "路径 %s\n" % fake_path, [])
        self.assertTrue([f for f in bad if f[1] == "personal-path"], bad)
        bad = cs.scan_text("x.md", "机器人 QQ %s 的日志\n" % ("1234" + "56789"), ["123456789"])
        self.assertTrue([f for f in bad if f[1] == "denylist"], bad)
        # 3) 占位符与豁免注释不该误报
        self.assertEqual([f for f in cs.scan_text(
            "x.py", 'api_key = "%s..."  # 示例\n' % "sk-", []) if f[0] == "hard"], [])
        self.assertEqual(cs.scan_text(
            "x.py", 'token = "%s"  # secret-scan: allow\n' % ("abcdefgh" + "12345678"), []), [])

    def test_beats_are_separate_files_and_state_writes_locked(self):
        """心跳各写各的文件(多进程不再互相覆盖), state_set 拿锁(残留锁能被抢回)。

        背景: 四个服务同时读改写 state.json —— Windows 上报 WinError 32 直接把启动搞失败,
        其它平台上则是"后写的把先写的整份覆盖",表现为心跳/计数忽然回退(CI 偶发就是它)。
        """
        core.ensure_dirs()
        # 1) 心跳: 每个服务一个文件, 互不干扰
        core.beat("collector", 111)
        core.beat("web", 222)
        self.assertEqual(int(open(core.beat_path("collector"), encoding="utf-8").read()), 111)
        self.assertEqual(int(open(core.beat_path("web"), encoding="utf-8").read()), 222)
        self.assertGreaterEqual(core.last_beat("collector"), 111)
        self.assertNotEqual(core.beat_path("collector"), core.beat_path("web"))
        # 2) state_set 写完后不该留下锁文件
        core.state_set("unit_test_key", {"n": 1})
        self.assertEqual(core.state_get("unit_test_key"), {"n": 1})
        self.assertFalse(os.path.exists(os.path.join(core.RUN_DIR, ".state.lock")),
                         "锁文件没被清掉")
        # 3) 残留锁(进程崩了留下)要能被抢回, 不能死等
        lockp = os.path.join(core.RUN_DIR, ".state.lock")
        with open(lockp, "w", encoding="utf-8") as f:
            f.write("999999")
        old = time.time() - 60
        os.utime(lockp, (old, old))
        t0 = time.time()
        core.state_set("unit_test_key", {"n": 2})          # 超时/抢锁都该很快返回
        self.assertLess(time.time() - t0, 6.0, "残留锁把自己锁死了")
        self.assertEqual(core.state_get("unit_test_key"), {"n": 2})
        try:
            os.remove(lockp)
        except OSError:
            pass

    def test_ci_annotate_extracts_failures(self):
        """CI 的失败整理工具本身要有测试 —— 它坏了会让"看不到失败原因"雪上加霜。

        (背景: 曾把这段逻辑以 heredoc 内嵌在 workflow 里, YAML 缩进一错 bash 就 exit 2,
        全平台一起挂还查不到原因。)
        """
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import ci_annotate as ca                                      # noqa: E402

        log = ("test_ok (m.T) ... ok\n"
               "FAIL: test_a (m.T)\n"
               "----------------------------------------------------------------------\n"
               "Traceback (most recent call last):\n"
               '  File "/x/tests/smoke.py", line 42, in test_a\n'
               "    self.assertTrue(False)\n"
               "AssertionError: False is not true\n")
        items = ca.parse(log)
        self.assertEqual(len(items), 1, items)
        self.assertIn("test_a", items[0][0])
        self.assertIn("line 42", items[0][1])
        self.assertIn("AssertionError", items[0][1])
        self.assertEqual(ca.esc("a%b\nc"), "a%25b%0Ac")
        # 完全没有 FAIL 行(启动期就挂)时, parse 返回空 —— main() 会退化成"贴日志尾部"
        self.assertEqual(ca.parse("boom\nImportError: no module\n"), [])

    def test_non_linux_fallbacks_and_ownership_safety(self):
        """macOS/BSD 没有 /proc: RSS/命令行/父进程要走 `ps` 兜底, 端口占用走 `lsof`。

        顺带锁住一条安全语义: **读不到对方环境变量时绝不能认领那个进程** ——
        认错人就会去杀别人的进程(macOS 上真实踩到: 端口占用被误判成"我们自己人")。
        """
        import builtins

        real_run, real_env, real_open = core._run, core._pid_environ, builtins.open
        real_iswin = core._is_windows
        core._is_windows = lambda: False        # 假装是 macOS/BSD, 才跑得到 ps/lsof 兜底
        try:
            # 1) proc_rss: /proc 不可用时退回 ps -o rss=(单位 KB)
            core._run = lambda cmd, timeout=20: "  2048\n" if "rss=" in " ".join(cmd) else ""
            builtins.open = lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("/proc"))
            self.assertEqual(core.proc_rss(1234), 2048 * 1024)
            # 2) etime: ps 的 [[dd-]hh:]mm:ss 要能解析成 HH:MM:SS
            core._run = lambda cmd, timeout=20: "01:02:03\n"
            rss, etime = core._rss_and_etime(4321)
            self.assertEqual(etime, "01:02:03", etime)
            builtins.open = real_open
            # 3) 命令行: ps -o command=
            core._run = lambda cmd, timeout=20: "python -m qqops.webapp --port 8091\n"
            self.assertIn("qqops.webapp", core._cmdline(9999))
            # 4) 父进程: ps -o ppid= (直接测兜底函数 —— Linux 上 /proc 分支优先, 走不到这里)
            core._run = lambda cmd, timeout=20: "42\n"
            self.assertIn(42, core._ancestor_pids_ps())
            # 5) lsof 解析: p<pid> / c<command>
            core._run = lambda cmd, timeout=20: "p777\ncnginx\nn*:8091\n"
            own = core._port_owner_lsof(8091)
            self.assertEqual(own["pid"], 777)
            self.assertEqual(own["cmd"], "nginx")
            # 6) 安全语义: 环境变量读不到 → 不算"我们自己人"
            core._pid_environ = lambda pid: {}
            core._run = lambda cmd, timeout=20: "12345\n"
            self.assertEqual(core._own_pids("qqops.webapp"), [],
                             "读不到环境变量时不能认领进程(否则可能误杀别人的进程)")
            # 7) pid 文件是本机安装自己的记录: 即使 env 读不到(macOS), 也要能证明"这是我们的进程"
            core.ensure_dirs()
            pidf = core.pid_path("collector")
            try:
                with open(pidf, "w", encoding="utf-8") as f:
                    f.write(str(os.getpid()))
                self.assertIn(os.getpid(), core._pids_from_pidfiles("qqops.collector"),
                              "pid 文件里记着的进程应当被认作自己人")
                self.assertIn(os.getpid(), core._pids_from_pidfiles(),
                              "不按模块过滤时也要能看到 pid 文件里的进程")
            finally:
                try:
                    os.remove(pidf)
                except OSError:
                    pass
        finally:
            core._run, core._pid_environ = real_run, real_env
            core._is_windows = real_iswin
            builtins.open = real_open

    def test_console_encoding_never_breaks_chinese(self):
        """非 UTF-8 控制台(西文 Windows 是 cp1252/cp437)下, 打印中文不能把程序打崩。

        踩过的坑: CI 的 windows-latest 上, 服务启动日志/`qqops doctor`/测试自己打印的中文
        都抛 UnicodeEncodeError —— 本地中文 Windows(cp936)完全看不出来。
        修法: 每个入口先 `core.ensure_utf8_console()`, 独立工具脚本自带同样的 3 行。
        """
        for stream in (sys.stdout, sys.stderr):
            enc = str(getattr(stream, "encoding", "") or "").lower().replace("-", "")
            self.assertEqual(enc, "utf8", "标准流没有切到 UTF-8: %r" % stream)
        # 每个进程入口都必须调用(新增入口忘了调用时, 这条测试会挡住)
        for rel in ("qqops/collector.py", "qqops/webapp.py", "qqops/scheduler.py",
                    "qqops/watchdog.py", "qqops/runner.py", "qqops/svc.py", "qqops/tools.py"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                self.assertIn("ensure_utf8_console()", f.read(), "%s 没有切 UTF-8 控制台" % rel)
        # 独立工具/打包脚本自带实现(它们不 import qqops)
        for rel in ("pack.py", "tools/check_secrets.py", "tools/verify_package.py",
                    "tools/changelog_notes.py"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                self.assertIn("_utf8_console", f.read(), "%s 没有切 UTF-8 控制台" % rel)

    def test_docs_site_nav_has_no_dead_links(self):
        """文档站的导航/侧边栏指向的文件必须真实存在(死导航比死链更讨厌)。"""
        cfg = os.path.join(ROOT, "docs", ".vitepress", "config.mts")
        if not os.path.exists(cfg):
            self.skipTest("没有文档站配置")
        with open(cfg, encoding="utf-8") as f:
            text = f.read()
        links = set(re.findall(r"link:\s*'([^']+)'", text))
        self.assertGreater(len(links), 10, "导航条目太少, 配置可能没被解析")
        for link in links:
            if link.startswith("http"):
                continue
            rel = link.strip("/").split("#")[0]
            cand = os.path.join(ROOT, "docs", rel + ".md")
            cand2 = os.path.join(ROOT, "docs", rel, "index.md")
            self.assertTrue(os.path.exists(cand) or os.path.exists(cand2),
                            "导航指向不存在的页面: %s" % link)
        # 站点的首页与几个使用文档必须在
        for rel in ("docs/index.md", "docs/guide/quickstart.md", "docs/guide/cli.md",
                    "docs/guide/console.md", "docs/guide/config.md", "docs/guide/security.md",
                    "docs/guide/troubleshooting.md", "docs/guide/plugins.md"):
            self.assertTrue(os.path.exists(os.path.join(ROOT, rel)), "缺少 %s" % rel)

    def test_docs_and_packaging_are_consistent(self):
        """文档/打包清单/CI 互相引用一致 —— 发布前最容易出现的"漏打包/死链接"。"""
        need = ["README.md", "README_EN.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md",
                "CODE_OF_CONDUCT.md", "CHANGELOG.md", "docs/DEPLOY.md", "docs/FAQ.md",
                "docs/ARCHITECTURE.md", "docs/CONSOLE.md", "docs/INGEST.md", "docs/POWER.md",
                "docs/CHAT.md", "tools/check_secrets.py", "tools/verify_package.py",
                "tools/console_probe.js", "config.example.json"]
        if os.path.isdir(os.path.join(ROOT, ".git")):
            # .github/ 只在开发检出里(发布包不带 CI 配置)
            need.append(".github/workflows/ci.yml")
        for rel in need:
            self.assertTrue(os.path.exists(os.path.join(ROOT, rel)), "缺少 %s" % rel)
        # README 里指向仓库内的链接必须真实存在
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        for m in re.finditer(r"\]\((?!#)([^)]+)\)", readme):
            link = m.group(1).split("#")[0]
            if link.startswith(("http", "mailto")):
                continue
            self.assertTrue(os.path.exists(os.path.join(ROOT, link)), "README 死链: %s" % link)
        # pack.py 的清单必须覆盖所有根级文档
        sys.path.insert(0, ROOT)
        import pack as pack_mod                                      # noqa: E402
        for rel in ("README.md", "README_EN.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md",
                    "CODE_OF_CONDUCT.md", "docs", "tools"):
            self.assertIn(rel, pack_mod.ITEMS, "pack.py 清单缺少 %s" % rel)

    def test_changelog_notes_extracts_the_release_section(self):
        """Release 正文取自 CHANGELOG: 抽取器要准 —— 抽错/抽串会让发布页写错版本。"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import changelog_notes as cn                                 # noqa: E402
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
            cur = f.read().strip()
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
            text = f.read()
        body = cn.extract(text, cur)
        self.assertTrue(body, "CHANGELOG 里缺少当前版本 %s 的段落" % cur)
        self.assertNotIn("## [", body, "%s 的段落把下一个版本也带上了" % cur)
        self.assertEqual(cn.extract(text, "v" + cur), body, "v 前缀应当等价")
        # 每个版本都要能取到, 且互不相同(抽串了就会出现重复内容)
        vers = re.findall(r"^##\s+\[?(\d+\.\d+\.\d+)\]?", text, re.M)
        self.assertGreaterEqual(len(vers), 10, "CHANGELOG 版本记录太少, 抽取器可能没被真正验证")
        seen = {}
        for v in vers:
            b = cn.extract(text, v)
            self.assertTrue(b, "取不到 %s 的段落" % v)
            seen[v] = b
        self.assertEqual(len(set(seen.values())), len(seen), "不同版本抽出了相同内容")
        # 不存在的版本: 明确失败, 绝不返回半截内容
        self.assertEqual(cn.extract(text, "99.99.99"), "")

    def test_secret_scan_covers_untracked_files(self):
        """打包门禁必须扫"工作区"(含**未跟踪**文件) —— 包打的是工作区, 不是 git 索引。"""
        if not os.path.isdir(os.path.join(ROOT, ".git")):
            self.skipTest("不是 git 检出")
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import check_secrets as cs                                  # noqa: E402
        with open(os.path.join(ROOT, "pack.py"), encoding="utf-8") as f:
            self.assertIn("--tree", f.read(), "pack.py 的打包前扫描没覆盖未跟踪文件")
        tracked, tree = set(cs.tracked_files()), set(cs.tree_files())
        self.assertTrue(tracked <= tree, "工作区清单漏掉了已跟踪文件")
        # 被 gitignore 的目录不该被扫: 开发草稿里必然有真 QQ 号, 扫了只会变成假警报
        for bad in ("data/", "dist/", "node_modules/", "_probe/", "__pycache__/"):
            self.assertFalse([p for p in tree if p.startswith(bad)],
                             "工作区扫描把 gitignore 的 %s 也扫了" % bad)
        # 新文件(未跟踪)要在清单里, 而且真能被扫出问题(退出码 1)
        probe = os.path.join(ROOT, "_scan_probe_test.txt")
        fake = "ghp_" + "A" * 30                 # 拼出来: 这一行本身不能命中扫描规则
        with open(probe, "w", encoding="utf-8") as f:
            f.write("token = %s\n" % fake)
        self.addCleanup(lambda: os.path.exists(probe) and os.remove(probe))
        self.assertIn("_scan_probe_test.txt", set(cs.tree_files()))
        p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "check_secrets.py"),
                            "--tree", "--quiet"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1,
                         "未跟踪文件里的密钥没被拦下: %s" % (p.stdout or "")[-300:])

    def test_install_docs_are_copy_pasteable(self):
        """安装教程必须能**整段复制粘贴**: 不许出现 <版本号> 这类要人手动替换的占位符。"""
        docs = ["README.md", "README_EN.md", "docs/guide/quickstart.md", "docs/DEPLOY.md",
                "docs/guide/backup.md"]
        # 只针对"要人替换才能用"的占位符; HTML 标签、CLI 用法表(如 <type>(<scope>))不在其中
        bad = re.compile(r"<\s*(版本|新版本|version|ver|token|令牌|QQ|你的QQ|自定义令牌)\s*>"
                         r"|qqops-<")
        # 命令里不许出现"带版本号的包名"(qqops-3.4.2.tar.gz / qqops-<版本>.tar.gz 都算);
        # qqops-latest.tar.gz 与用户自己生成的 qqops-data.tar.gz 不算
        vered = re.compile(r"qqops-\d+\.\d+|qqops-<")
        for rel in docs:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                text = f.read()
            hits = sorted({m.group(0) for m in bad.finditer(text)})
            self.assertEqual(hits, [], "%s 里还有要人手动替换的占位符: %s" % (rel, hits))
            self.assertIsNone(vered.search(text), "%s 的命令里还有带版本号的包名" % rel)
        # 下载网址必须是固定名字的那种(releases/latest + qqops-latest.tar.gz)
        url = "releases/latest/download/qqops-latest.tar.gz"
        for rel in ("README.md", "docs/guide/quickstart.md", "docs/DEPLOY.md",
                    "docs/guide/backup.md"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                self.assertIn(url, f.read(), "%s 的安装/升级步骤没有用固定网址" % rel)

    def test_release_publishes_a_fixed_name_asset(self):
        """文档写死了 releases/latest/download/qqops-latest.tar.gz —— 发布流程必须真的产出它。"""
        path = os.path.join(ROOT, ".github", "workflows", "release.yml")
        if not os.path.exists(path):
            self.skipTest("发布包不带 CI 配置")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("qqops-latest.tar.gz", text,
                      "release.yml 没有产出固定名字的发布包, 文档里的固定网址会 404")
        self.assertIn("releases/latest/download", text, "固定网址的由来没有写在发布流程里")
        # 校验和要覆盖两个包(用户下载固定名字的那个也要能核对)
        self.assertIn("sha256sum qqops-*.tar.gz", text, "SHA256SUMS 没有覆盖固定名字的包")

    def test_installer_survives_a_non_termux_environment(self):
        """install.sh 必须在**没有 Termux 环境**的普通 Linux 上跑完。

        真机验收时抓到的 bug: 注册命令那一步写了 `"$PREFIX/bin"`, 而 `$PREFIX` 只有 Termux 才有,
        脚本又是 `set -u` —— Linux 用户照文档安装会在第 5/7 步**当场退出**:
        程序复制了、配置生成了, 但服务没启动、结束提示也没打印。
        """
        if not shutil.which("bash"):
            self.skipTest("没有 bash(Windows 上正常跳过)")
        # Windows 上 `where bash` 找到的是 WSL 启动器, 装了发行版才能用 —— 先探一下
        try:
            probe = subprocess.run(["bash", "-c", "echo ok"], capture_output=True,
                                   text=True, timeout=60)
        except Exception as e:
            self.skipTest("bash 跑不起来: %s" % e)
        if probe.returncode != 0 or "ok" not in (probe.stdout or ""):
            self.skipTest("bash 不可用(Windows 的 WSL 启动器不算)")
        tmp = tempfile.mkdtemp(prefix="qqops-install-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ)
        env.pop("PREFIX", None)                    # 模拟 Linux: 没有 Termux 的 PREFIX
        env["HOME"] = tmp                          # 别污染真实 HOME(安装器会写 ~/.termux/boot/)
        env["QQOPS_HOME"] = os.path.join(tmp, "qqops")
        p = subprocess.run(["bash", os.path.join(ROOT, "install.sh"), "--no-start"],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        out = (p.stdout or "") + (p.stderr or "")
        self.assertNotIn("unbound variable", out, "安装器引用了未定义的变量:\n%s" % out[-600:])
        self.assertEqual(p.returncode, 0, "安装器在无 Termux 环境下失败:\n%s" % out[-800:])
        self.assertTrue(os.path.exists(os.path.join(tmp, "qqops", "data", "config.json")),
                        "安装器没有生成配置:\n%s" % out[-600:])
        self.assertIn("安装完成", out, "安装器没有跑到结束提示:\n%s" % out[-600:])

    def test_docs_site_anchors_are_valid(self):
        """站内 `#锚点` 必须真的存在 —— 锚点错了构建不报, 用户点进去只是"没跳转"。"""
        dist = os.path.join(ROOT, "docs", ".vitepress", "dist")
        if not os.path.isdir(dist):
            self.skipTest("没有文档站构建产物(先 npm run docs:build)")
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import check_docs_anchors as cda                              # noqa: E402
        problems = cda.check()
        self.assertEqual(problems, [], "站内锚点失效: %s" % problems[:5])
        # 文档站工作流要跑这个检查, 否则本地不构建就没人查
        wf = os.path.join(ROOT, ".github", "workflows", "docs.yml")
        if os.path.exists(wf):
            with open(wf, encoding="utf-8") as f:
                self.assertIn("check_docs_anchors.py", f.read(),
                              "docs.yml 没有跑锚点检查")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib 默认自动跟随 302, 会掩盖"未鉴权应跳登录"的断言语义。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# ---------------------------------------------------------------- v2.0 新能力
class TestExtract(unittest.TestCase):
    def test_parse_json_tolerates_fences_and_noise(self):
        self.assertEqual(extract.parse_json('```json\n{"a":1}\n```'), {"a": 1})
        self.assertEqual(extract.parse_json('前面废话 {"a":1,} 后面废话'), {"a": 1})
        self.assertEqual(extract.parse_json("不是 json"), {})
        self.assertEqual(extract.parse_json(""), {})

    def test_normalize_limits_and_filters(self):
        obj = {"overview": "x" * 300,
               "events": [{"title": "甲", "kind": "deadline", "due": "明天", "confidence": 0.9},
                          {"title": "乙", "confidence": 0.1},          # 低置信被丢
                          {"title": "", "confidence": 0.9}],           # 无标题被丢
               "todos": ["写周报", {"what": "交作业", "due": "2026-11-01"}],
               "links": [{"url": "https://a.com", "title": "T"}, {"url": "ftp://bad"}],
               "facts": ["事实一"], "memory_update": "记忆"}
        n = extract.normalize(obj, {"max_events": 5, "low_confidence_drop": 0.35}, 7)
        self.assertEqual(len(n["events"]), 1)
        self.assertEqual(n["events"][0]["group_id"], 7)
        self.assertGreater(n["events"][0]["due_at"], core.now())
        self.assertEqual(len(n["todos"]), 2)
        self.assertEqual(n["todos"][0]["kind"], "todo")
        self.assertEqual([l["url"] for l in n["links"]], ["https://a.com"])
        self.assertEqual(n["facts"], ["事实一"])
        self.assertEqual(n["memory_update"], "记忆")
        self.assertLessEqual(len(n["overview"]), 80)

    def test_normalize_caps_kind(self):
        n = extract.normalize({"events": [{"title": "x", "kind": "乱写", "confidence": 0.9}]}, {}, 1)
        self.assertEqual(n["events"][0]["kind"], "event")


class TestParseDue(unittest.TestCase):
    def test_relative_and_absolute(self):
        day = 86400
        for kw in ("今天", "明天", "后天", "3天后", "本周五", "月底", "2026-12-31", "12-31"):
            self.assertGreater(core.parse_due(kw), core.now(), "%s 应解析为未来时间" % kw)
        self.assertGreater(core.parse_due("明天"), core.now() + day - 7200)
        self.assertGreater(core.parse_due("后天"), core.now() + 2 * day - 7200)
        self.assertGreater(core.parse_due("3天后"), core.now() + 3 * day - 7200)
        self.assertGreater(core.parse_due("下周三"), core.now() + 6 * day)
        self.assertLessEqual(core.parse_due("下周三") - core.now(), 14 * day)

    def test_due_is_end_of_day(self):
        v = core.parse_due("明天")
        self.assertEqual(time.strftime("%H:%M", time.localtime(v)), "23:59")

    def test_explicit_time_is_kept(self):
        v = core.parse_due("2026-12-31 08:30")
        self.assertTrue(v)
        self.assertEqual(time.strftime("%m-%d %H:%M", time.localtime(v)), "12-31 08:30")

    def test_unknown_returns_zero(self):
        for s in ("", "不知道", "以后", "尽快", None):
            self.assertEqual(core.parse_due(s), 0)


class TestRender(unittest.TestCase):
    def _sections(self):
        return [{"group_id": 5, "name": "群A", "stats": "- 消息总数: **3**", "overview": "概览",
                 "topics": [{"title": "话题", "summary": "摘要"}], "facts": ["事实"],
                 "events": [{"kind": "deadline", "title": "交稿", "detail": "周五前",
                             "due_at": core.now() + 86400, "priority": 1, "relevant": True}],
                 "todos": [{"what": "写周报", "who": "我", "due_at": 0}],
                 "links": [{"url": "https://a.com", "title": "T", "why": "值得看"}],
                 "memory": "记忆", "error": ""}]

    def test_xlsx_is_valid_zip_with_parts(self):
        import io
        import zipfile
        data = render.xlsx_bytes([["标题", "数"], ["a", 1]], "表1")
        self.assertTrue(data.startswith(b"PK"))
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        for need in ("[Content_Types].xml", "xl/workbook.xml", "xl/worksheets/sheet1.xml"):
            self.assertIn(need, names)
        multi = render.xlsx_multi({"事件": [["a"]], "链接": [["b"]]})
        self.assertIn("xl/worksheets/sheet2.xml", zipfile.ZipFile(io.BytesIO(multi)).namelist())

    def test_docx_is_valid_zip(self):
        import io
        import zipfile
        data = render.docx_bytes([("h1", "标题"), ("p", "正文 <&>"), ("bullet", "项")], "t")
        self.assertTrue(data.startswith(b"PK"))
        z = zipfile.ZipFile(io.BytesIO(data))
        doc = z.read("word/document.xml").decode()
        self.assertIn("标题", doc)
        self.assertIn("&lt;&amp;&gt;", doc)          # XML 转义正确

    def test_md_to_docx_blocks(self):
        blocks = render.md_to_docx_blocks("# 一\n## 二\n- 三\n> 四\n正文")
        self.assertEqual(blocks[0], ("h1", "一"))
        self.assertEqual(blocks[1], ("h2", "二"))
        self.assertEqual(blocks[2], ("bullet", "三"))
        self.assertEqual(blocks[3], ("quote", "四"))

    def test_reports_contain_key_bits(self):
        secs, meta = self._sections(), {"stamp": "s", "hours": 24, "generated": core.now(),
                                        "n_events": 1, "n_todos": 1, "n_links": 1,
                                        "events_open": 1, "due_7d": 1, "overdue": 0, "due_list": []}
        md = render.report_md(meta, secs)
        self.assertIn("重要事件", md)
        self.assertIn("交稿", md)
        html_doc = render.report_html(meta, secs, "# 档案")
        self.assertIn("<!doctype html>", html_doc.lower())
        self.assertIn("交稿", html_doc)
        self.assertIn("个人档案", html_doc)
        digest = render.digest_text(meta, secs)
        self.assertIn("交稿", digest)
        self.assertLessEqual(len(digest), 1200)

    def test_events_rows_header(self):
        rows = render.events_rows([{"title": "t", "kind": "deadline", "status": "open"}])
        self.assertEqual(rows[0][0], "状态")
        self.assertEqual(len(rows), 2)


class TestLinkfetch(unittest.TestCase):
    def test_ssrf_guard(self):
        for bad in ("http://localhost/x", "http://127.0.0.1/x", "http://192.168.1.1/x",
                    "http://10.0.0.5/x", "http://172.16.0.1/x", "http://169.254.1.1/x",
                    "file:///etc/passwd", "ftp://a.com/x", "gopher://a.com",
                    "http://[::1]/x", "not-a-url"):
            ok, why = linkfetch._host_ok(bad)
            self.assertFalse(ok, "%s 应被拒绝(%s)" % (bad, why))

    def test_html_to_text(self):
        title, text = linkfetch.html_to_text(
            "<html><head><title>标题A</title><style>a{}</style></head>"
            "<body><nav>导航</nav><p>正文一</p><p>正文二</p><script>x=1</script></body></html>")
        self.assertEqual(title, "标题A")
        self.assertIn("正文一", text)
        self.assertNotIn("x=1", text)
        self.assertNotIn("导航", text)

    def test_extract_and_filter_urls(self):
        urls = linkfetch.extract_urls("看 https://a.com/x 和 https://b.com/y)，。 ")
        self.assertEqual(urls, ["https://a.com/x", "https://b.com/y"])
        self.assertFalse(linkfetch.interesting("https://multimedia.nt.qq.com.cn/download?a=1"))
        self.assertFalse(linkfetch.interesting("https://x.com/a.jpg"))
        self.assertTrue(linkfetch.interesting("https://mp.weixin.qq.com/s/abc"))
        self.assertEqual(linkfetch.domain_of("https://a.com/x/y"), "a.com")

    def test_fetch_rejects_internal_without_network(self):
        r = linkfetch.fetch("http://127.0.0.1:8091/", timeout=2)
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"])


class TestFileread(unittest.TestCase):
    def test_kind_of(self):
        self.assertEqual(fileread.kind_of("a.docx"), "docx")
        self.assertEqual(fileread.kind_of("b.XLSX"), "xlsx")
        self.assertEqual(fileread.kind_of("c.pdf"), "pdf")
        self.assertEqual(fileread.kind_of("d.md"), "text")
        self.assertEqual(fileread.kind_of("e.png"), "image")
        self.assertEqual(fileread.kind_of("f.bin"), "other")

    def test_text_encodings(self):
        self.assertEqual(fileread.read_bytes("你好".encode("utf-8"), "a.txt")["text"], "你好")
        self.assertEqual(fileread.read_bytes("你好".encode("gb18030"), "a.txt")["text"], "你好")

    def test_xlsx_roundtrip(self):
        """用自己产出的 xlsx 反过来解析 —— 同时验证写入与读取。"""
        data = render.xlsx_bytes([["姓名", "分数"], ["张三", 90], ["李四", 85]], "成绩")
        r = fileread.read_bytes(data, "成绩.xlsx")
        self.assertEqual(r["kind"], "xlsx")
        self.assertIn("张三", r["text"])
        self.assertIn("李四", r["text"])

    def test_docx_roundtrip(self):
        data = render.docx_bytes([("h1", "方案"), ("p", "第一步 收集需求"), ("bullet", "联系设计")], "t")
        r = fileread.read_bytes(data, "方案.docx")
        self.assertIn("收集需求", r["text"])
        self.assertIn("联系设计", r["text"])

    def test_pptx_minimal(self):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("ppt/slides/slide1.xml",
                       '<p:sld xmlns:a="x"><a:p><a:t>第一页要点</a:t></a:p></p:sld>')
        r = fileread.read_bytes(buf.getvalue(), "演示.pptx")
        self.assertIn("第一页要点", r["text"])

    def test_pdf_reports_error_without_dependency(self):
        """没有 pypdf 时给出明确提示; 装了 pypdf 时对坏 PDF 也应报错而不是崩溃。"""
        r = fileread.read_bytes(b"%PDF-1.4 fake", "a.pdf")
        self.assertTrue(r["error"], "坏 PDF 必须有明确错误")
        self.assertEqual(r["text"], "")
        self.assertEqual(r["kind"], "pdf")

    def test_bad_zip_does_not_crash(self):
        r = fileread.read_bytes(b"not a zip", "x.docx")
        self.assertTrue(r["error"])


class TestMediaHelpers(unittest.TestCase):
    def test_from_segment(self):
        info = media.from_segment({"type": "image",
                                   "data": {"url": "http://x/y.jpg", "file": "y.jpg", "file_size": "1234"}})
        self.assertEqual(info["url"], "http://x/y.jpg")
        self.assertEqual(info["size"], 1234)
        self.assertEqual(media.from_segment("bad"), {})

    def test_guess_mime_and_key(self):
        self.assertEqual(media.guess_mime("a.png"), "image/png")
        self.assertEqual(media.guess_mime("b", b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertEqual(len(media.key_of("http://x/y.jpg")), 24)
        self.assertEqual(media.safe_name("http://x/y.jpg", "报告.docx"), "报告.docx")

    def test_container_path_mapping(self):
        fake = os.path.join(TMP, "rootfs-a")
        orig = core._proot_rootfs
        core._proot_rootfs = lambda: fake
        try:
            p = media.container_path("/root/.config/QQ/a.png")
            self.assertTrue(p.startswith(fake), p)
            self.assertTrue(os.path.normpath(p).endswith(
                os.path.normpath(os.path.join("root", ".config", "QQ", "a.png"))), p)
            self.assertEqual(media.container_path("relative/x.png"), "")
            self.assertEqual(media.container_path(""), "")
        finally:
            core._proot_rootfs = orig

    def test_fetch_media_falls_back_to_napcat_cache(self):
        """QQ CDN 常会 400, 但 NapCat 已把图片落到容器里 → 直接读本地文件。"""
        root = os.path.join(TMP, "rootfs-b")
        target = os.path.join(root, "root", "cache", "img.png")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        data = b"\x89PNG\r\n\x1a\n" + b"0" * 100
        with open(target, "wb") as f:
            f.write(data)

        class FakeNC:
            def call(self, action, payload=None, timeout=None):
                return {"status": "ok", "retcode": 0,
                        "data": {"file": "/root/cache/img.png", "url": ""}}

        orig = core._proot_rootfs
        core._proot_rootfs = lambda: root
        try:
            r = media.fetch_media(FakeNC(), {"url": "", "file": "x.png"}, 1, 1_000_000, "image")
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["via"], "napcat-cache")
            self.assertEqual(r["size"], len(data))
            self.assertEqual(r["mime"], "image/png")
        finally:
            core._proot_rootfs = orig

    def test_find_in_napcat_cache_by_lowercase_name(self):
        """NapCat 缓存是小写 md5.ext 且按 YYYY-MM 分片; 消息段里给的常是大写。"""
        root = os.path.join(TMP, "rootfs-c")
        target = os.path.join(root, "root", ".config", "QQ", "nt_qq_abc", "nt_data", "Pic",
                              time.strftime("%Y-%m"), "Ori", "deadbeef00112233.png")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"1" * 50)
        orig = core._proot_rootfs
        core._proot_rootfs = lambda: root
        try:
            self.assertEqual(media.find_in_napcat_cache("DEADBEEF00112233.png", core.now()), target)
            self.assertEqual(media.find_in_napcat_cache("deadbeef0011", core.now()), target)
            self.assertEqual(media.find_in_napcat_cache("不存在的名字.png", core.now()), "")
            self.assertEqual(media.find_in_napcat_cache("../etc/passwd", core.now()), "")
            r = media.fetch_media(None, {"url": "", "file": "DEADBEEF00112233.png", "ts": core.now()},
                                  1, 1_000_000, "image")
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["via"], "cache-scan")
        finally:
            core._proot_rootfs = orig

    def test_fetch_media_reports_failure_cleanly(self):
        class BadNC:
            def call(self, *a, **k):
                raise RuntimeError("boom")
        r = media.fetch_media(BadNC(), {"url": "", "file": ""}, 1, 1000, "image")
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"])


class TestPlanStore(unittest.TestCase):
    """事件去重合并 / 状态流转 / 记忆 / 链接 / 媒体 的存储行为。"""

    def setUp(self):
        self.store = core.Store()
        self.gid = 8000 + core.now() % 1000

    def test_upsert_dedup_merges(self):
        ev = {"kind": "deadline", "title": "交设计稿", "group_id": self.gid,
              "due_at": core.now() + 86400, "priority": 2, "confidence": 0.6}
        eid1, created1 = self.store.event_upsert(dict(ev))
        ev2 = dict(ev, priority=1, confidence=0.9, detail="补充说明")
        eid2, created2 = self.store.event_upsert(ev2)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(eid1, eid2)
        row = [e for e in self.store.events(status="all", limit=500) if e["id"] == eid1][0]
        self.assertEqual(row["priority"], 1)
        self.assertEqual(row["detail"], "补充说明")
        self.assertEqual(round(row["confidence"], 2), 0.9)

    def test_status_flow_and_stats(self):
        eid, _ = self.store.event_upsert({"kind": "event", "title": "状态流转", "group_id": self.gid,
                                          "confidence": 0.9})
        self.assertTrue(self.store.event_update(eid, status="done"))
        row = [e for e in self.store.events(status="done", limit=500) if e["id"] == eid]
        self.assertEqual(len(row), 1)
        self.assertIn("done", self.store.event_stats()["by_status"])
        self.store.event_delete(eid)

    def test_due_soon_and_overdue_query(self):
        eid, _ = self.store.event_upsert({"kind": "deadline", "title": "明天到期", "group_id": self.gid,
                                          "due_at": core.now() + 3600, "confidence": 0.9})
        self.assertTrue(any(e["id"] == eid for e in self.store.events(status="open", soon_only=True, limit=200)))
        self.store.event_delete(eid)

    def test_memory_roundtrip(self):
        self.store.memory_set("group:%d" % self.gid, "digest", "记忆A")
        self.assertEqual(self.store.memory_get("group:%d" % self.gid)["content"], "记忆A")
        self.store.memory_set("group:%d" % self.gid, "digest", "记忆B", bump_hits=True)
        rec = self.store.memory_get("group:%d" % self.gid)
        self.assertEqual(rec["content"], "记忆B")
        self.assertGreaterEqual(rec["hits"], 1)
        self.assertTrue(any(m["scope"] == "group:%d" % self.gid for m in self.store.memory_all("group:")))

    def test_link_and_media(self):
        url = "https://example.com/%d" % core.now()
        self.store.link_upsert(url, title="T", domain="example.com", group_id=self.gid)
        self.store.link_update(url, status="ok", excerpt="正文摘要", fetched_at=core.now())
        rec = [l for l in self.store.links(limit=50) if l["url"] == url][0]
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["excerpt"], "正文摘要")
        k = "key%d" % core.now()
        self.store.media_upsert(k, group_id=self.gid, kind="image", url=url, size=10, status="downloaded")
        self.store.media_upsert(k, caption="一只猫", caption_at=core.now(), status="captioned")
        m = self.store.media_get(k)
        self.assertEqual(m["caption"], "一只猫")
        self.assertEqual(m["size"], 10)
        self.assertTrue(any(x["key"] == k for x in self.store.media_list(limit=50)))

    def test_profile_roundtrip(self):
        old = core.profile_read()
        try:
            core.profile_write("# 测试档案\n- 目标: X")
            self.assertIn("测试档案", core.profile_read())
        finally:
            core.profile_write(old)


class TestSchedule(unittest.TestCase):
    """三种调度模式的下次时间计算 + 配置去重迁移 + 新消息计数。"""

    def test_daily_next_run(self):
        sch = {"enabled": True, "mode": "daily", "times": ["23:59"]}
        r = core.next_run(sch)
        self.assertEqual(r["mode"], "daily")
        self.assertGreater(r["ts"], core.now())
        self.assertLessEqual(r["in"], 86400)
        self.assertIn("每天", r["label"])

    def test_interval_next_run(self):
        core.state_set("last_summary_ts", core.now() - 100)      # 100 秒前跑过
        sch = {"enabled": True, "mode": "interval", "every_minutes": 60}
        r = core.next_run(sch)
        self.assertEqual(r["mode"], "interval")
        self.assertGreater(r["ts"], core.now())
        self.assertLessEqual(r["in"], 60 * 60 + 5)
        self.assertIn("每隔", r["label"])

    def test_interval_past_due_rolls_forward(self):
        core.state_set("last_summary_ts", core.now() - 3 * 3600)
        r = core.next_run({"enabled": True, "mode": "interval", "every_minutes": 60})
        self.assertLessEqual(r["in"], 5, "已经超时的间隔任务应当立刻(≤5 秒)到期")

    def test_weekly_next_run_respects_weekdays(self):
        wd = time.localtime().tm_wday + 1
        sch = {"enabled": True, "mode": "weekly", "weekdays": [wd], "weekly_time": "23:59"}
        r = core.next_run(sch)
        self.assertEqual(r["mode"], "weekly")
        self.assertGreater(r["ts"], core.now())
        self.assertLessEqual(r["in"], 7 * 86400)
        self.assertIn("每周", r["label"])
        # 只选明天时, 结果必须落在"明天"这一天(而不是今天)
        tomorrow = time.localtime(core.now() + 86400).tm_wday + 1
        if tomorrow != wd:
            r2 = core.next_run({"enabled": True, "mode": "weekly", "weekdays": [tomorrow],
                                "weekly_time": "23:59"})
            self.assertEqual(time.localtime(r2["ts"]).tm_wday + 1, tomorrow)
            self.assertGreater(r2["ts"], core.now())

    def test_disabled_and_off(self):
        r = core.next_run({"enabled": False, "mode": "daily", "times": ["23:59"]})
        self.assertEqual(r["ts"], 0)
        self.assertEqual(r["label"], "已停用")

    def test_next_run_time_backward_compatible(self):
        r = core.next_run_time(["23:59"])
        self.assertGreater(r["ts"], core.now())

    def test_count_since(self):
        st = core.Store()
        t0 = core.now()
        before = st.count_since(t0)
        st.insert_message({"time": t0 + 1, "message_type": "group", "group_id": 1, "user_id": 1,
                           "raw_message": "计数测试", "sender": {"nickname": "x"}, "message": "x"})
        self.assertEqual(st.count_since(t0), before + 1)
        self.assertEqual(st.count_since(core.now() + 60), 0)

    def test_migrate_dedupes_schedule_hours(self):
        raw = json.load(open(core.CONF_PATH, encoding="utf-8-sig"))
        raw.setdefault("schedule", {})["hours"] = 12
        raw["summarizer"]["window_hours"] = 24
        core.atomic_write(core.CONF_PATH, json.dumps(raw, ensure_ascii=False, indent=2))
        hand = core.Config.__new__(core.Config)      # 直接测 migrate(), 避开 load() 自动迁移
        hand.path, hand.data = core.CONF_PATH, json.loads(json.dumps(raw))
        notes = hand.migrate()
        self.assertTrue(any("hours" in n for n in notes), notes)
        again = core.Config()
        self.assertNotIn("hours", again.data["schedule"], "重复项应被清除")
        self.assertEqual(again.data["summarizer"]["window_hours"], 12, "旧值应合并到时间窗")
        again.set_values({"summarizer": {"window_hours": 24}})

    def test_migrate_drops_unknown_schedule_keys(self):
        cfg = core.Config()
        raw = json.load(open(core.CONF_PATH, encoding="utf-8-sig"))
        raw["schedule"]["legacy_junk"] = "x"
        core.atomic_write(core.CONF_PATH, json.dumps(raw, ensure_ascii=False, indent=2))
        core.Config().migrate()
        self.assertNotIn("legacy_junk", core.Config().data["schedule"])

    def test_schedule_apply_validates(self):
        cfg = core.Config()
        bad = webapp.schedule_apply(cfg, {"mode": "hourly"})
        self.assertFalse(bad["ok"])
        self.assertTrue(bad["errors"])
        bad2 = webapp.schedule_apply(cfg, {"times": ["25:99:x"]})
        self.assertFalse(bad2["ok"])
        bad3 = webapp.schedule_apply(cfg, {"every_minutes": 0})
        self.assertFalse(bad3["ok"])
        bad4 = webapp.schedule_apply(cfg, {"weekdays": []})
        self.assertFalse(bad4["ok"])
        good = webapp.schedule_apply(cfg, {"mode": "interval", "every_minutes": 180})
        self.assertTrue(good["ok"], good)
        self.assertEqual(good["schedule"]["mode"], "interval")
        self.assertEqual(good["schedule"]["every_minutes"], 180)
        self.assertIn("每隔", good["next"]["label"])
        webapp.schedule_apply(core.Config(), {"mode": "daily", "times": "22:00, 09:30"})
        self.assertEqual(core.Config().data["schedule"]["times"], ["22:00", "09:30"])


class TestIntegration(unittest.TestCase):
    """真实子进程: 采集器收消息落库 + SIGTERM 必须能退出(死锁回归)。"""

    def _env(self):
        env = dict(os.environ)
        env["QQOPS_DATA"] = TMP
        env["TZ"] = "Asia/Shanghai"
        env["PYTHONIOENCODING"] = "utf-8"      # 子进程打印中文不能崩(西文 Windows)
        env["QQOPS_DEBUG_START"] = "7"         # 启动卡住就把线程栈打出来(CI 只能靠它看真相)
        return env

    @staticmethod
    def _why_not_listening(proc, log_name: str) -> str:
        """子进程没起来时, 把能拿到的线索凑成一段人话(CI 上日志看不到, 全靠它)。"""
        parts = []
        try:
            if proc.poll() is not None:
                parts.append("子进程已退出, 退出码 %s" % proc.returncode)
            else:
                parts.append("子进程仍在运行(pid %s): 多半卡住了, 线程栈见上面的 QQOPS_DEBUG_START 输出"
                             % proc.pid)
        except Exception:
            pass
        try:
            # 进程还在跑时直接 read() 会阻塞 → 先终止, 再把管道里已有的内容读出来
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
            out = (proc.stdout.read() if proc.stdout else "")[-2000:]
            parts.append("子进程输出:\n%s" % (out or "(空)"))
        except Exception as e:
            parts.append("读子进程输出失败: %s" % e)
        try:
            logf = os.path.join(TMP, "logs", "%s.log" % log_name)
            if os.path.exists(logf):
                with open(logf, encoding="utf-8", errors="replace") as f:
                    parts.append("日志 %s 末尾:\n%s" % (log_name, f.read()[-1500:]))
            else:
                parts.append("没有日志文件 %s" % logf)
        except Exception as e:
            parts.append("读日志失败: %s" % e)
        return "\n".join(parts)

    def test_collector_roundtrip_and_shutdown(self):
        port = free_port()
        proc = subprocess.Popen([sys.executable, "-m", "qqops.collector", "--port", str(port)],
                                cwd=ROOT, env=self._env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        try:
            if not wait_port(port):
                proc.kill()
                out = (proc.stdout.read() if proc.stdout else "")[-1500:]
                self.fail("采集器未在超时内监听\n%s"
                          % ci_note(self._why_not_listening(proc, "collector"),
                                          "采集器未启动"))
            ev = {"post_type": "message", "message_type": "group", "group_id": 4242,
                  "user_id": 7, "message_id": 4242001, "time": int(time.time()),
                  "raw_message": "集成测试 头像 讨论", "sender": {"nickname": "集成"},
                  "message": [{"type": "text", "data": {"text": "x"}}]}
            req = urllib.request.Request("http://127.0.0.1:%d/" % port,
                                         data=json.dumps(ev).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                self.assertEqual(r.status, 200)
            with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=8) as r:
                health = json.loads(r.read().decode())
            self.assertTrue(health["ok"])
            self.assertGreaterEqual(health["counters"]["ingested"], 1)
            found = core.Store().messages(group="4242", q="头像", hours=1)
            self.assertEqual(found["total"], 1, "FTS 未检索到刚采集的消息")
        finally:
            if POSIX:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    self.fail("采集器收到 SIGTERM 后未退出(信号处理死锁回归)")
            else:
                proc.terminate()
                proc.wait(timeout=8)

    def test_webapp_http_contract(self):
        port = free_port()
        cfg = core.Config()
        token = cfg.data["web"]["token"]
        proc = subprocess.Popen([sys.executable, "-m", "qqops.webapp", "--port", str(port)],
                                cwd=ROOT, env=self._env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        try:
            if not wait_port(port):
                proc.kill()
                out = (proc.stdout.read() if proc.stdout else "")[-1500:]
                self.fail("控制台未在超时内监听\n%s"
                          % ci_note(self._why_not_listening(proc, "web"),
                                          "控制台未启动"))
            base = "http://127.0.0.1:%d" % port
            opener = urllib.request.build_opener(_NoRedirect)

            with urllib.request.urlopen(base + "/health", timeout=8) as r:
                self.assertEqual(r.status, 200)
                self.assertTrue(json.loads(r.read().decode())["ok"])

            # 未鉴权: 页面 302 跳登录, 接口 401 JSON
            try:
                opener.open(base + "/", timeout=8)
                self.fail("未鉴权访问首页应 302")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 302)
            try:
                urllib.request.urlopen(base + "/api/status", timeout=8)
                self.fail("未鉴权访问 API 应 401")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 401)

            # 带令牌: 正常返回, 且 HTML 禁缓存(浏览器缓存旧页面曾是线上事故)
            req = urllib.request.Request(base + "/?token=" + token)
            with urllib.request.urlopen(req, timeout=8) as r:
                body = r.read().decode()
                self.assertIn("QQOps", body)
                self.assertEqual(r.headers.get("Cache-Control"), "no-store")

            req = urllib.request.Request(base + "/api/status", headers={"X-Token": token})
            with urllib.request.urlopen(req, timeout=15) as r:
                snap = json.loads(r.read().decode())
            self.assertIn("services", snap)
            self.assertIn("extras", snap)

            # HEAD 必须支持(探活/缓存校验)
            req = urllib.request.Request(base + "/health", method="HEAD")
            with urllib.request.urlopen(req, timeout=8) as r:
                self.assertEqual(r.status, 200)

            # 静态资源: 单一 Cache-Control, 不能与 no-store 叠加
            req = urllib.request.Request(base + "/static/app.js?v=" + webapp.ASSET_VER)
            with urllib.request.urlopen(req, timeout=8) as r:
                cc = r.headers.get_all("Cache-Control") or []
                self.assertEqual(len(cc), 1, "Cache-Control 重复: %s" % cc)
                self.assertNotIn("no-store", cc[0])
                self.assertIn("max-age", cc[0])

            # ---- v3 框架与控制台接口(插件路由表 + 容错 + 日志流) ----
            def get(path):
                rq = urllib.request.Request(base + path, headers={"X-Token": token})
                with urllib.request.urlopen(rq, timeout=20) as rr:
                    return json.loads(rr.read().decode())

            def post(path, payload=None):
                data = json.dumps(payload or {}).encode()
                rq = urllib.request.Request(base + path, data=data,
                                            headers={"X-Token": token,
                                                     "Content-Type": "application/json"})
                with urllib.request.urlopen(rq, timeout=30) as rr:
                    return json.loads(rr.read().decode())

            fw = get("/api/app/status")
            self.assertIn("plugins", fw)
            self.assertIn("handlers", fw)
            self.assertIn("bus", fw)
            self.assertEqual(fw.get("roles"), ["console"])
            self.assertGreaterEqual(fw["handlers"].get("route", 0), 10, fw["handlers"])

            pl = get("/api/plugins")
            self.assertIn("plugins", pl)
            ids = [p["id"] for p in pl["plugins"]]
            self.assertIn("reminders", ids, pl)

            cfgv = get("/api/plugins/config?id=reminders")
            self.assertTrue(cfgv.get("fields"), cfgv)
            paths = {f["path"] for f in cfgv["fields"]}
            self.assertIn("basic.enabled", paths)

            logs = get("/api/app/log?since=0")
            self.assertIn("entries", logs)
            self.assertIn("modules", logs)

            crash = get("/api/app/crashes?limit=5")
            self.assertIn("stats", crash)
            self.assertIn("items", crash)

            dash = get("/api/ops/dashboard")
            self.assertIn("services", dash)
            self.assertIn("stats", dash)
            self.assertIn("partial", dash)

            selfcheck = get("/api/app/selftest")
            self.assertIn("items", selfcheck)
            self.assertTrue(selfcheck["items"])

            # 插件命令要能真跑通(走注册表 → 插件方法)
            res = post("/api/plugins/command", {"name": "reminders.due"})
            self.assertTrue(res.get("ok"), res)

            # 故意触发的异常必须进账本, 而不是把控制台打崩
            before = get("/api/app/crashes?limit=1")["stats"]["total"]
            try:
                urllib.request.urlopen(urllib.request.Request(
                    base + "/api/app/errors_test", data=b"{}",
                    headers={"X-Token": token, "Content-Type": "application/json"}), timeout=15)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 500, "插件接口异常应返回 500 而不是崩掉")
                payload = json.loads(e.read().decode())
                self.assertFalse(payload.get("ok"))
                self.assertTrue(payload.get("rid"), payload)
            kinds = [r["kind"] for r in get("/api/app/crashes?limit=10")["items"]]
            self.assertIn("route_error", kinds, kinds)
            self.assertGreaterEqual(get("/api/app/crashes?limit=1")["stats"]["total"], before)

            # 停用/启用插件: 路由要跟着消失再出现
            off = post("/api/plugins/toggle", {"id": "reminders", "enabled": False})
            self.assertTrue(off.get("ok"), off)
            rq = urllib.request.Request(base + "/api/plugins/reminders/list", headers={"X-Token": token})
            try:
                urllib.request.urlopen(rq, timeout=10)
                self.fail("停用插件后其路由应不可用")
            except urllib.error.HTTPError as e:
                self.assertIn(e.code, (404, 500))
            on = post("/api/plugins/toggle", {"id": "reminders", "enabled": True})
            self.assertEqual(on.get("status"), "loaded")
            self.assertIn("items", get("/api/plugins/reminders/list"))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_cli_tools_run(self):
        env = self._env()
        for args in (["-m", "qqops.tools", "init"], ["-m", "qqops.tools", "doctor"]):
            p = subprocess.run([sys.executable] + args, cwd=ROOT, env=env,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=90)
            out = (p.stdout or "") + (p.stderr or "")
            self.assertIn(p.returncode, (0, 1), "命令失败: %s\n%s" % (args, out[-500:]))
            self.assertTrue(out.strip(), "命令没有输出: %s" % args)


class TestFramework(unittest.TestCase):
    """框架内核: 事件 / 总线 / 管线 / 注册表 / schema / 路由 / 容错。"""

    def _isolated_ledger(self, name):
        path = os.path.join(TMP, "crashes_%s.jsonl" % name)
        if os.path.exists(path):
            os.remove(path)
        return fw_errors.CrashLedger(path), path

    # ------------------------------------------------------------ 事件与总线
    def test_event_helper_and_fields(self):
        evt = fw_event.Event.message({"message_type": "group", "group_id": 42,
                                      "user_id": 7, "time": 1700000000})
        self.assertEqual(evt.kind, "message.received")
        self.assertEqual(evt.session, "group:42")
        self.assertEqual(evt.ts, 1700000000.0)
        self.assertFalse(evt.dropped)
        evt.drop("测试")
        self.assertTrue(evt.dropped and evt.drop_reason == "测试")
        self.assertIn("payload_keys", evt.to_dict())

    def test_bus_isolates_bad_handler_and_stage(self):
        """一个插件抛异常, 其它插件照样收到事件 —— 这是容错的底线。"""
        reg = fw_registry.Registry()
        seen = []

        def bad(evt, ctx):
            raise RuntimeError("这个插件坏了")

        def good(evt, ctx):
            seen.append(evt.id)

        def bad_stage(evt, ctx):
            raise ValueError("阶段炸了")

        def good_stage(evt, ctx):
            evt.add_result("ok")

        reg.add(fw_registry.Ref(kind="event", name="x.y", fn=bad, plugin_id="bad"))
        reg.add(fw_registry.Ref(kind="event", name="x.y", fn=good, plugin_id="good"))
        reg.add(fw_registry.Ref(kind="stage", name="s1", fn=bad_stage, plugin_id="bad", priority=10))
        reg.add(fw_registry.Ref(kind="stage", name="s2", fn=good_stage, plugin_id="good", priority=20))
        pipe = fw_pipeline.Pipeline(registry=reg)
        bus = fw_bus.EventBus(registry=reg, pipeline=pipe)
        ld, path = self._isolated_ledger("bus")
        old = fw_errors.LEDGER
        fw_errors.LEDGER = ld
        try:
            evt = bus.dispatch(fw_event.Event("x.y", {"a": 1}))
            self.assertEqual(seen, [evt.id], "坏处理器把好处理器带崩了")
            self.assertIn("ok", evt.results)
            self.assertFalse(evt.dropped)
            kinds = [r["kind"] for r in ld.recent(10)]
            self.assertIn("stage_error", kinds)
            self.assertIn("handler_error", kinds)
        finally:
            fw_errors.LEDGER = old
            if os.path.exists(path):
                os.remove(path)

    def test_pipeline_critical_stage_drops_event(self):
        reg = fw_registry.Registry()
        calls = []

        def critical(evt, ctx):
            raise RuntimeError("落库失败")

        def later(evt, ctx):
            calls.append("later")

        reg.add(fw_registry.Ref(kind="stage", name="persist", fn=critical, priority=20,
                                meta={"critical": True}))
        reg.add(fw_registry.Ref(kind="stage", name="enrich", fn=later, priority=40))
        pipe = fw_pipeline.Pipeline(registry=reg)
        ld, path = self._isolated_ledger("crit")
        old = fw_errors.LEDGER
        fw_errors.LEDGER = ld
        try:
            evt = pipe.run(fw_event.Event("message.received"))
            self.assertTrue(evt.dropped, "关键阶段失败必须丢弃事件")
            self.assertEqual(calls, [], "关键阶段失败后不该继续跑后续阶段")
            self.assertIn("关键阶段", evt.drop_reason)
        finally:
            fw_errors.LEDGER = old
            if os.path.exists(path):
                os.remove(path)

    def test_pipeline_stage_timing_recorded(self):
        reg = fw_registry.Registry()
        reg.add(fw_registry.Ref(kind="stage", name="t", fn=lambda e, c: None, priority=10))
        pipe = fw_pipeline.Pipeline(registry=reg)
        evt = pipe.run(fw_event.Event("k"))
        notes = [n for n in evt.meta["stages"] if n.get("stage") == "t"]
        self.assertTrue(notes and "ms" in notes[0], evt.meta)

    # ------------------------------------------------------------ schema
    def test_schema_defaults_validate_and_coerce(self):
        sch = {"g": {"type": "object", "description": "组", "items": {
            "on": {"type": "bool", "description": "开关", "default": True},
            "n": {"type": "int", "description": "数量", "default": 5, "min": 1, "max": 10},
            "mode": {"type": "string", "description": "模式", "default": "a", "options": ["a", "b"]},
            "words": {"type": "list", "description": "词", "default": []},
        }}}
        self.assertEqual(fw_schema.defaults(sch), {"g": {"on": True, "n": 5, "mode": "a", "words": []}})
        clean, errs = fw_schema.validate(sch, {"g": {"on": "no", "n": "99", "mode": "c",
                                                     "words": "a, b"}})
        self.assertFalse(clean["g"]["on"])
        self.assertEqual(clean["g"]["n"], 5, "越界必须回退默认值")
        self.assertEqual(clean["g"]["mode"], "a", "非法枚举必须回退默认值")
        self.assertEqual(clean["g"]["words"], ["a", "b"])
        self.assertEqual(len(errs), 2, errs)
        # 未知键保留(插件可能自己存状态)
        clean2, _ = fw_schema.validate(sch, {"g": {"extra": 1}, "top": 2})
        self.assertEqual(clean2["g"]["extra"], 1)
        self.assertEqual(clean2["top"], 2)

    def test_schema_flatten_for_ui(self):
        sch = {"g": {"type": "object", "description": "组", "items": {
            "k": {"type": "string", "description": "密钥", "default": "", "secret": True,
                  "hint": "填了才生效"}}}}
        rows = fw_schema.flatten(sch)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], "g.k")
        self.assertTrue(rows[0]["secret"])
        self.assertEqual(rows[0]["group_label"], "组")
        self.assertEqual(rows[0]["hint"], "填了才生效")

    # ------------------------------------------------------------ 路由
    def test_router_match_exact_and_param(self):
        r = fw_web.Router()
        r.add("/api/a", lambda req, ctx: {"ok": 1})
        r.add("/api/item/<rid>", lambda req, ctx: {"rid": "x"}, methods=("GET", "POST"))
        hit, params = r.match("GET", "/api/a")
        self.assertTrue(hit)
        self.assertEqual(params, {})
        hit, params = r.match("post", "/api/item/r001")
        self.assertTrue(hit)
        self.assertEqual(params, {"rid": "r001"})
        hit, _ = r.match("DELETE", "/api/item/r001")
        self.assertIsNone(hit)
        hit, _ = r.match("GET", "/api/nope")
        self.assertIsNone(hit)

    def test_request_and_response_helpers(self):
        req = fw_web.Request(method="POST", query={"a": ["1"]}, body=b'{"x": 2}')
        self.assertEqual(req.q("a"), "1")
        self.assertEqual(req.q("missing", "d"), "d")
        self.assertEqual(req.json(), {"x": 2})
        resp = fw_web.Response.json({"ok": True})
        self.assertEqual(resp.status, 200)
        self.assertIn(b'"ok"', resp.body)

    # ------------------------------------------------------------ 容错
    def test_crash_ledger_dedupe_redact_clear(self):
        ld, path = self._isolated_ledger("led")
        ld.record("comp.a", exc=ValueError("token=abcdef123456 出错了"))
        ld.record("comp.a", exc=ValueError("token=abcdef123456 出错了"))   # 窗口内去重
        rows = ld.load()
        self.assertEqual(len(rows), 1, "同 key 在窗口内应只落一条")
        self.assertNotIn("abcdef123456", rows[0]["message"], "密钥必须脱敏")
        ld.record("comp.b", exc=TypeError("另一种错"))
        st = ld.stats()
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["by_exception"].get("ValueError"), 1)
        self.assertEqual(ld.clear(), 2)
        self.assertEqual(ld.load(), [])
        if os.path.exists(path):
            os.remove(path)

    def test_guard_decorator_and_context_manager(self):
        ld, path = self._isolated_ledger("guard")
        old = fw_errors.LEDGER
        fw_errors.LEDGER = ld
        try:
            @fw_errors.guard("t.deco", fallback=[])
            def boom():
                raise RuntimeError("x")
            self.assertEqual(boom(), [])

            @fw_errors.guard("t.re", fallback=None, reraise=True)
            def boom2():
                raise RuntimeError("y")
            self.assertRaises(RuntimeError, boom2)

            ran = []
            with fw_errors.guard("t.ctx"):
                ran.append(1)
                raise ValueError("吞掉我")
            self.assertEqual(ran, [1])
            self.assertEqual(ld.stats()["total"], 3)

            with fw_errors.guard("t.ctx2", reraise=True):
                self.assertRaises(ValueError, _raise_inside)
        finally:
            fw_errors.LEDGER = old
            if os.path.exists(path):
                os.remove(path)

    def test_retry_then_success_and_exhaust(self):
        calls = {"n": 0}

        @fw_errors.retry(times=3, delay=0.01, component="t.retry")
        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ValueError("还没好")
            return "ok"
        self.assertEqual(flaky(), "ok")
        self.assertEqual(calls["n"], 3)

        ld, path = self._isolated_ledger("retry")
        old = fw_errors.LEDGER
        fw_errors.LEDGER = ld
        try:
            @fw_errors.retry(times=2, delay=0.01, component="t.retry2")
            def always():
                raise RuntimeError("永远失败")
            self.assertRaises(RuntimeError, always)
            kinds = [r["kind"] for r in ld.recent(5)]
            self.assertIn("retry_exhausted", kinds)
        finally:
            fw_errors.LEDGER = old
            if os.path.exists(path):
                os.remove(path)

    def test_backoff_grows_and_trips(self):
        b = fw_errors.Backoff(base=1.0, factor=2.0, cap=10.0, window=60, trip=3)
        self.assertFalse(b.tripped("s"))
        self.assertEqual(b.fail("s"), 1.0)
        self.assertEqual(b.fail("s"), 2.0)
        self.assertEqual(b.fail("s"), 4.0)
        self.assertTrue(b.tripped("s"), "同窗口内失败达阈值必须熔断")
        self.assertFalse(b.due("s"))
        st = b.state("s")
        self.assertEqual(st["attempt"], 3)
        b.reset("s")
        self.assertFalse(b.tripped("s"))
        self.assertTrue(b.due("s"))

    def test_install_global_hooks(self):
        old = sys.excepthook
        try:
            fw_errors.install_global_hooks(component="t.hooks")
            self.assertIsNot(sys.excepthook, old)
        finally:
            sys.excepthook = old

    def test_log_broker_cursor(self):
        br = fw_log.LogBroker(size=10)
        br.publish("m1", "INFO", "第一条")
        br.publish("m2", "WARN", "第二条")
        br.publish("m1", "ERROR", "第三条")
        got = br.since(0)
        self.assertEqual(len(got["entries"]), 3)
        self.assertEqual(got["next"], 3)
        got2 = br.since(got["next"])
        self.assertEqual(got2["entries"], [])
        only = br.since(0, module="m1")
        self.assertEqual(len(only["entries"]), 2)
        warn = br.since(0, level="WARN")
        self.assertEqual(len(warn["entries"]), 2, "WARN 及以上应包含 ERROR")
        self.assertEqual(len(br.tail(1)), 1)
        self.assertEqual(br.state()["cap"], 10)
        self.assertEqual(sorted(m["module"] for m in br.modules()), ["m1", "m2"])
        self.assertEqual(br.clear(), 3)
        # 环形缓冲: 超出容量后序号继续增长(clear 不清零序号, 免得客户端游标错乱)
        base = br.state()["seq"]
        for i in range(15):
            br.publish("m", "INFO", "x%d" % i)
        self.assertEqual(br.state()["size"], 10)
        info = br.since(0)
        self.assertEqual(info["entries"][0]["seq"], base + 6, "只应保留最后 10 条")
        self.assertEqual(info["dropped"], base + 5, "被环挤掉的条数要如实上报")


def _raise_inside():
    raise ValueError("re-raise 测试")


class TestPluginSystem(unittest.TestCase):
    """插件体系: 加载/隔离/配置/任务/命令/路由/页面/热重载。"""

    def setUp(self):
        # 每个用例一份干净的插件数据/配置, 避免互相串味
        for sub in ("plugin_data", "config"):
            shutil.rmtree(os.path.join(TMP, sub), ignore_errors=True)

    def _isolated_ledger(self, name):
        path = os.path.join(TMP, "crashes_%s.jsonl" % name)
        if os.path.exists(path):
            os.remove(path)
        return fw_errors.CrashLedger(path), path

    def _app(self, roles=("all",), dirs=None):
        app = fw_app.QQOpsApp(name="test", roles=roles,
                              plugin_dirs=dirs if dirs is not None else [PLUGIN_DIR],
                              workers=1)
        app.setup()
        return app

    def test_builtin_reminders_plugin_loads(self):
        app = self._app()
        try:
            st = app.plugins.state()
            self.assertGreaterEqual(st["total"], 1, st)
            row = [p for p in st["plugins"] if p["id"] == "reminders"]
            self.assertTrue(row, st["plugins"])
            self.assertEqual(row[0]["status"], "loaded", row[0])
            self.assertGreaterEqual(row[0]["handlers"], 5, "提醒插件应注册阶段/任务/命令/路由/页面")
            counts = app.registry.counts()
            self.assertGreaterEqual(counts.get("stage", 0), 1)
            self.assertGreaterEqual(counts.get("task", 0), 1)
            self.assertGreaterEqual(counts.get("route", 0), 3)
            self.assertGreaterEqual(counts.get("page", 0), 1)
        finally:
            app.stop()

    def test_reminders_extracts_time_from_message(self):
        app = self._app(roles=("ingest",))
        try:
            evt = fw_event.Event.message({
                "post_type": "message", "message_type": "group", "group_id": 111,
                "user_id": 222, "message_id": 1, "time": int(time.time()),
                "raw_message": "记得下周三交实验报告 [CQ:at,qq=1]",
                "sender": {"nickname": "小明"}})
            app.dispatch_now(evt)
            inst = app.plugins.plugins["reminders"]["inst"]
            self.assertTrue(os.path.isfile(inst.inbox_path), "识别到的待办应进入收件箱")
            with open(inst.inbox_path, encoding="utf-8") as f:
                item = json.loads(f.readline())
            self.assertIn("实验报告", item["title"])
            self.assertGreater(item["due"], time.time())
            self.assertEqual(item["session"], "group:111")
            self.assertEqual(item["sender"], "小明")
        finally:
            app.stop()

    def test_reminders_ignores_noise(self):
        app = self._app(roles=("ingest",))
        try:
            inst = app.plugins.plugins["reminders"]["inst"]
            for text in ("哈哈哈哈", "交", "今天天气不错", "明年见"):
                evt = fw_event.Event.message({
                    "message_type": "group", "group_id": 1, "user_id": 2,
                    "raw_message": text, "time": int(time.time())})
                app.dispatch_now(evt)
            self.assertFalse(os.path.isfile(inst.inbox_path),
                             "噪声消息不该产生待办: %s" % text)
        finally:
            app.stop()

    def test_reminders_tick_notifies_and_dedupes(self):
        app = self._app(roles=("ingest", "tasks"))
        try:
            inst = app.plugins.plugins["reminders"]["inst"]
            now = int(time.time())
            # 一条刚到点、一条早就过期(要能区分"到点"与"过期")
            rows = [{"title": "交报告", "due": now - 10, "session": "group:9", "group": 9,
                     "user": 1, "sender": "我", "created": now - 100, "exact": True},
                    {"title": "体检", "due": now - 7200, "session": "group:9", "group": 9,
                     "user": 1, "sender": "我", "created": now - 9000, "exact": True}]
            os.makedirs(os.path.dirname(inst.inbox_path), exist_ok=True)
            with open(inst.inbox_path, "a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            res1 = inst.tick(app.ctx)
            self.assertEqual(res1["added"], 2)
            self.assertEqual(res1["notified"], 2, res1)
            # 第二次 tick: 不能重复提醒
            res2 = inst.tick(app.ctx)
            self.assertEqual(res2["notified"], 0, res2)
            st = inst._load()
            status = {i["title"]: i["status"] for i in st["items"]}
            self.assertEqual(status.get("交报告"), "notified")
            self.assertEqual(status.get("体检"), "late")
            self.assertIn("未开启推送", st["items"][0]["push_result"])
        finally:
            app.stop()

    def test_reminders_scan_command_reads_store(self):
        """补扫命令必须真的能从数据库里读出消息并抽出待办。

        (这条用例是为了记住一次真实的翻车: 命令里用了不存在的 Store 方法,
         因为没有测试覆盖"命令 → 数据库"这条链路, 直到真机上点了才发现。)
        """
        app = self._app(roles=("console", "tasks"))
        try:
            store = app.ctx.store
            before = len(store.messages(hours=6, page=1, size=5).get("rows") or [])
            store.insert_message({
                "message_type": "group", "group_id": 555, "user_id": 9, "message_id": 4242,
                "time": int(time.time()), "raw_message": "提醒我明天下午3点提交材料",
                "message": [{"type": "text", "data": {"text": "提醒我明天下午3点提交材料"}}],
                "sender": {"nickname": "补扫测试"}})
            res = app.plugins.run_command("reminders.scan", {"hours": 6}, app.ctx)
            self.assertTrue(res["ok"], res)
            out = res["result"]
            self.assertTrue(out.get("ok", True), out)
            self.assertGreater(out.get("scanned", 0), before, out)
            self.assertGreaterEqual(out.get("found", 0), 1, out)
            inst = app.plugins.plugins["reminders"]["inst"]
            self.assertTrue(os.path.isfile(inst.inbox_path))
            with open(inst.inbox_path, encoding="utf-8") as f:
                first = json.loads(f.readline())
            self.assertIn("提交材料", first["title"])
        finally:
            app.stop()

    def test_reminders_routes_and_commands(self):
        app = self._app(roles=("console",))
        try:
            hit, params = app.router.match("GET", "/api/plugins/reminders/list")
            self.assertTrue(hit, "插件的路由应注册到路由表")
            req = fw_web.Request(method="GET", path="/api/plugins/reminders/list")
            out = hit.fn(req, app.ctx)
            self.assertIn("items", out)
            self.assertIn("open", out)
            self.assertTrue(app.plugins.commands())
            names = [c["command"] for c in app.plugins.commands()]
            self.assertIn("reminders.due", names)
            res = app.plugins.run_command("reminders.due", {}, app.ctx)
            self.assertTrue(res["ok"], res)
            self.assertTrue(app.plugins.pages(), "插件应能往控制台加页面")
        finally:
            app.stop()

    def test_plugin_config_schema_and_save(self):
        app = self._app(roles=("console",))
        try:
            view = app.plugins.config_view("reminders")
            self.assertTrue(view["fields"])
            paths = {f["path"] for f in view["fields"]}
            self.assertIn("basic.enabled", paths)
            self.assertIn("notify.push", paths)
            cfg = app.plugins.config("reminders")
            self.assertTrue(cfg["basic"]["enabled"])
            ok, errs, saved = app.plugins.set_config("reminders", {"basic": {"max_days_ahead": 9999}})
            self.assertFalse(ok, "越界应报错")
            self.assertEqual(saved["basic"]["max_days_ahead"], 60, "越界应回退默认值")
            ok2, _, saved2 = app.plugins.set_config("reminders", {"notify": {"push": True}})
            self.assertTrue(ok2, saved2)
            self.assertTrue(app.plugins.config("reminders", force=True)["notify"]["push"])
            # 密钥掩码
            app.plugins.set_config("reminders", {"notify": {"target_session": "group:123"}})
            v2 = app.plugins.config_view("reminders")
            self.assertEqual(v2["config"]["notify"]["target_session"], "group:123")
        finally:
            app.stop()

    def test_manager_isolates_broken_plugin(self):
        """坏插件只影响它自己: 加载失败要记账, 其它插件照常可用。"""
        d = os.path.join(TMP, "plugin_src")
        shutil.rmtree(d, ignore_errors=True)
        for pid, code, meta in (
            ("broken", "import 这不是 python\n", {"id": "broken", "name": "坏插件"}),
            ("healthy", ("from qqops.framework.api import Plugin, register, command\n"
                         "@register(id='healthy', name='好插件')\n"
                         "class H(Plugin):\n"
                         "    @command('healthy.ping')\n"
                         "    def ping(self, args=None, ctx=None):\n"
                         "        return 'pong'\n"), {"id": "healthy", "name": "好插件"}),
        ):
            pdir = os.path.join(d, pid)
            os.makedirs(pdir, exist_ok=True)
            with open(os.path.join(pdir, "__init__.py"), "w", encoding="utf-8") as f:
                f.write(code)
            with open(os.path.join(pdir, "metadata.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
        ld, path = self._isolated_ledger("plug")
        old = fw_errors.LEDGER
        fw_errors.LEDGER = ld
        try:
            app = self._app(roles=("console",), dirs=[d])
            try:
                st = {p["id"]: p for p in app.plugins.state()["plugins"]}
                self.assertEqual(st["broken"]["status"], "failed", st["broken"])
                self.assertIn("SyntaxError", st["broken"]["error"] + st["broken"]["error"])
                self.assertEqual(st["healthy"]["status"], "loaded", st["healthy"])
                self.assertTrue(app.plugins.run_command("healthy.ping", {})["ok"])
                self.assertIn("load_failed", [r["kind"] for r in ld.recent(5)])
            finally:
                app.stop()
        finally:
            fw_errors.LEDGER = old
            shutil.rmtree(d, ignore_errors=True)
            if os.path.exists(path):
                os.remove(path)

    def test_plugin_enable_disable_and_reload(self):
        app = self._app(roles=("console",))
        try:
            self.assertEqual(app.plugins.disable("reminders"), "disabled")
            self.assertEqual(app.registry.handlers("route", "/api/plugins/reminders/list"), [])
            self.assertEqual(app.plugins.enable("reminders"), "loaded")
            self.assertTrue(app.registry.handlers("route", "/api/plugins/reminders/list"))
            self.assertEqual(app.plugins.reload("reminders"), "loaded")
            # 重复重载不应产生重复处理器
            rows = app.registry.handlers("command", "reminders.due")
            self.assertEqual(len(rows), 1, rows)
        finally:
            app.stop()
            app.plugins.enable("reminders")

    def test_app_status_shape(self):
        app = self._app(roles=("all",))
        try:
            app.start()
            st = app.status()
            for key in ("version", "roles", "plugins", "handlers", "bus", "crashes"):
                self.assertIn(key, st)
            self.assertIn("stages", st["bus"])
            self.assertTrue(st["plugins"]["total"] >= 1)
        finally:
            app.stop()

    def test_role_filtering(self):
        """按角色注册: 采集进程不该注册控制台路由, 控制台进程不该注册阶段。"""
        ingest = self._app(roles=("ingest",))
        try:
            self.assertFalse(ingest.router.match("GET", "/api/plugins/reminders/list")[0])
            self.assertTrue(ingest.registry.handlers("stage", "reminders_scan"))
        finally:
            ingest.stop()
        console = self._app(roles=("console",))
        try:
            self.assertTrue(console.router.match("GET", "/api/plugins/reminders/list")[0])
            self.assertFalse(console.registry.handlers("stage", "reminders_scan"))
        finally:
            console.stop()
        # 清掉残留注册, 避免影响其它用例
        app = self._app(roles=("all",))
        app.stop()


class TestIngestSpec(unittest.TestCase):
    """消息接收处理规范(OneBot v11): 逐条验证 docs/INGEST.md 里的约定。

    用一个真实采集器子进程 + 各种上报体, 覆盖: 去重、机器人自身消息、撤回、
    心跳、体积上限、非法 JSON、未知类型、空消息、chunked。
    """

    @staticmethod
    def _env():
        env = dict(os.environ)
        env["QQOPS_DATA"] = TMP
        env["TZ"] = "Asia/Shanghai"
        env["PYTHONIOENCODING"] = "utf-8"      # 子进程打印中文不能崩(西文 Windows)
        env["QQOPS_DEBUG_START"] = "7"         # 启动卡住就把线程栈打出来(CI 只能靠它看真相)
        return env

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.proc = subprocess.Popen([sys.executable, "-m", "qqops.collector", "--port", str(cls.port)],
                                    cwd=ROOT, env=cls._env(), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        if not wait_port(cls.port):
            cls.proc.kill()
            raise unittest.SkipTest("采集器未在超时内启动")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.proc.terminate()
            cls.proc.wait(timeout=8)
        except Exception:
            cls.proc.kill()

    def post(self, ev, raw: bytes = None, headers: dict = None, expect: int = 200):
        body = raw if raw is not None else json.dumps(ev, ensure_ascii=False).encode()
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request("http://127.0.0.1:%d/" % self.port, data=body, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.status, expect)
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, expect, "期望 HTTP %s, 实际 %s" % (expect, e.code))
            return json.loads(e.read().decode() or "{}")

    def counters(self) -> dict:
        with urllib.request.urlopen("http://127.0.0.1:%d/" % self.port, timeout=8) as r:
            return json.loads(r.read().decode())["counters"]

    def test_09_private_published_but_not_stored(self):
        """私聊默认**不入库**(隐私), 但仍要交给插件 —— 否则"在 QQ 里问 AI"收不到消息。

        这是"聊天内智能查询"的入口条件: 采集器不能因为"不存私聊"就把事件一起丢掉。
        """
        from qqops import collector

        published = []

        class FakeApp:
            def publish(self, event, wait=True):
                published.append((event.kind, dict(event.payload)))

        def handle(ev, cfg):
            h = object.__new__(collector.Handler)
            h.cfg, h.store, h.app = cfg, core.Store(), FakeApp()
            h.counters = dict(collector.Handler.counters)
            h._on_message(ev, "message")

        ev = {"post_type": "message", "message_type": "private", "user_id": 5,
              "message_id": 555001, "time": int(time.time()), "raw_message": "问 谁要交报告",
              "message": [{"type": "text", "data": {"text": "问 谁要交报告"}}],
              "sender": {"nickname": "我"}}
        handle(dict(ev), core.Config())
        self.assertEqual(len(published), 1, "私聊消息必须发到总线上(供插件响应)")
        self.assertEqual(published[0][0], "message.received")
        self.assertTrue(published[0][1].get("_not_stored"), "要标记未入库, 下游才不会再存一次")
        rows = core.Store().messages(hours=24, size=200)["rows"]
        self.assertEqual([r for r in rows if int(r.get("message_id") or 0) == 555001], [],
                         "私聊默认不应入库")
        # 关掉开关后, 私聊事件也不再交给插件(省内存/省事)
        published.clear()
        cfg2 = core.Config()
        cfg2.set_values({"collector": {"command_private": False}})
        handle(dict(ev), cfg2)
        self.assertEqual(published, [], "command_private=false 时不应发布私聊事件")

    def test_01_message_ingested_and_deduped(self):
        """正常消息入库; 同一条(重连补发)必须被去重拦住。"""
        ev = {"post_type": "message", "message_type": "group", "group_id": 9001,
              "user_id": 11, "message_id": 77001, "time": int(time.time()),
              "raw_message": "规范测试 去重 关键词",
              "message": [{"type": "text", "data": {"text": "规范测试 去重 关键词"}}],
              "sender": {"nickname": "甲"}}
        before = core.Store().messages(hours=1, size=1)["total"]
        self.post(ev)
        same = core.Store().messages(group="9001", q="规范测试 去重", hours=1)
        self.assertEqual(same["total"], 1, "第一次上报应入库")
        self.post(ev)                                    # 重连补发同一条
        again = core.Store().messages(group="9001", q="规范测试 去重", hours=1)
        self.assertEqual(again["total"], 1, "重复消息必须被唯一索引拦住")
        self.assertGreaterEqual(self.counters()["duplicate"], 1, "重复计数应+1")
        self.assertGreaterEqual(self.counters()["ingested"], 1)
        self.assertGreaterEqual(core.Store().messages(hours=1, size=1)["total"], before)

    def test_02_empty_message_skipped(self):
        """空消息(无 raw_message 也无 message)不入库。"""
        ev = {"post_type": "message", "message_type": "group", "group_id": 9001,
              "user_id": 11, "message_id": 77002, "time": int(time.time()),
              "raw_message": "", "message": [], "sender": {"nickname": "甲"}}
        skipped = self.counters()["skipped"]
        self.post(ev)
        self.assertGreater(self.counters()["skipped"], skipped, "空消息应计入 skipped")

    def test_03_self_message_stored_with_flag(self):
        """机器人自己发的消息(message_sent)也要入库并标记 self=1。"""
        ev = {"post_type": "message_sent", "message_type": "group", "group_id": 9001,
              "self_id": 1127487015, "user_id": 1127487015, "message_id": 77003,
              "time": int(time.time()), "raw_message": "这是机器人自己发的话",
              "message": [{"type": "text", "data": {"text": "这是机器人自己发的话"}}],
              "sender": {"nickname": "黯"}}
        self.post(ev)
        rows = core.Store().messages(group="9001", q="机器人自己发的话", hours=1)["rows"]
        self.assertEqual(len(rows), 1, "机器人自身消息应入库")
        self.assertEqual(int(rows[0]["self"]), 1, "必须标记 self=1")
        self.assertGreaterEqual(self.counters()["sent"], 1)

    def test_04_recall_marks_message(self):
        """notice/group_recall 必须把对应消息标记为已撤回。"""
        ev = {"post_type": "message", "message_type": "group", "group_id": 9002,
              "user_id": 12, "message_id": 88001, "time": int(time.time()),
              "raw_message": "这条等下会被撤回",
              "message": [{"type": "text", "data": {"text": "这条等下会被撤回"}}],
              "sender": {"nickname": "乙"}}
        self.post(ev)
        self.post({"post_type": "notice", "notice_type": "group_recall", "group_id": 9002,
                   "user_id": 12, "operator_id": 12, "message_id": 88001,
                   "time": int(time.time())})
        rows = core.Store().messages(group="9002", q="等下会被撤回", hours=1)["rows"]
        self.assertEqual(int(rows[0]["recalled"]), 1, "撤回事件应标记消息")
        only_live = core.Store().messages(group="9002", q="等下会被撤回", hours=1,
                                          exclude_recalled=True)
        self.assertEqual(only_live["total"], 0, "排除撤回后应查不到")
        self.assertGreaterEqual(self.counters()["notice"], 1)

    def test_05_heartbeat_recorded(self):
        """meta_event/heartbeat 要写心跳, 这是 NapCat 连接状态的可靠信号。"""
        core.state_set("napcat_beat", 0)
        self.post({"post_type": "meta_event", "meta_event_type": "heartbeat", "interval": 5000,
                   "status": {"online": True, "good": True}, "time": int(time.time())})
        beat = int(core.state_get("napcat_beat", 0) or 0)
        self.assertGreater(beat, 0, "心跳应写入 state")
        self.assertLess(abs(core.now() - beat), 30)
        self.assertGreaterEqual(self.counters()["meta"], 1)

    def test_06_bad_payloads(self):
        """畸形上报: 非法 JSON → 400; 未知 post_type → 200 但跳过; 超大体积 → 413(不看体就拒)。"""
        self.post({}, raw=b"{not json", expect=400)
        c0 = self.counters()["skipped"]
        self.post({"post_type": "something_new", "foo": 1}, expect=200)
        self.assertGreater(self.counters()["skipped"], c0)
        # 超大体积: 只发请求头(声明 9MB), 服务端应当立刻回 413 而不是先收完 9MB
        head = ("POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                "Content-Length: 9437184\r\nConnection: close\r\n\r\n").encode()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(head)
            resp = s.recv(512).decode("utf-8", "replace")
        finally:
            s.close()
        self.assertIn("413", resp.split("\r\n", 1)[0], resp[:200])

    def test_07_chunked_body_accepted(self):
        """NapCat 用 chunked 发请求体, 必须能正确消费。

        用裸 socket 发送: urllib 会自动加 Content-Length, 测不出真正的 chunked 路径。
        """
        ev = {"post_type": "message", "message_type": "group", "group_id": 9003,
              "user_id": 13, "message_id": 99001, "time": int(time.time()),
              "raw_message": "chunked 传输测试",
              "message": [{"type": "text", "data": {"text": "chunked 传输测试"}}],
              "sender": {"nickname": "丙"}}
        raw = json.dumps(ev, ensure_ascii=False).encode()
        mid = len(raw) // 2
        chunked = (b"%x\r\n" % mid) + raw[:mid] + b"\r\n" + \
                  (b"%x\r\n" % (len(raw) - mid)) + raw[mid:] + b"\r\n0\r\n\r\n"
        head = ("POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n").encode()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(head + chunked)
            resp = b""
            while len(resp) < 8192:
                part = s.recv(4096)
                if not part:
                    break
                resp += part
        finally:
            s.close()
        text = resp.decode("utf-8", "replace")
        self.assertIn("200", text.split("\r\n", 1)[0], text[:200])
        rows = core.Store().messages(group="9003", q="chunked 传输", hours=1)["rows"]
        self.assertEqual(len(rows), 1, "chunked 上报应正常入库")

    def test_08_kinds_normalized(self):
        """消息段类型要归一化(record→voice / mface→face / node→forward / flash→image)。"""
        ev = {"post_type": "message", "message_type": "group", "group_id": 9004,
              "user_id": 14, "message_id": 99002, "time": int(time.time()),
              "raw_message": "[CQ:record][CQ:mface][CQ:flash]",
              "message": [{"type": "record"}, {"type": "mface"}, {"type": "flash"},
                          {"type": "node"}, {"type": "text"}],
              "sender": {"nickname": "丁"}}
        self.post(ev)
        rows = core.Store().messages(group="9004", hours=1)["rows"]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["kinds"], "voice,face,image,forward,text", rows[0]["kinds"])


class TestAssistantPlugin(unittest.TestCase):
    """智能查询: 问题解析 → 检索 → 组装 → 模型作答 → 来源/会话/降级/额度。"""

    def setUp(self):
        for sub in ("plugin_data", "config"):
            shutil.rmtree(os.path.join(TMP, sub), ignore_errors=True)
        st = core.Store()
        con = st.conn()
        try:
            for t in ("chat_turns", "chat_sessions", "messages", "events", "memories",
                      "links", "media"):
                con.execute("DELETE FROM " + t)
            try:
                con.execute("DELETE FROM messages_fts")
            except Exception:
                pass
            con.commit()
        finally:
            con.close()

    def _app(self, roles=("console", "tasks")):
        app = fw_app.QQOpsApp(name="test", roles=roles, plugin_dirs=[PLUGIN_DIR], workers=1)
        app.setup()
        return app

    def _seed(self):
        st = core.Store()
        st.upsert_group(6666, "测试群")
        now = int(time.time())
        rows = [
            (101, 6666, "小明", "记得下周三交实验报告", "text"),
            (102, 6666, "小红", "报告我已经写完了, 晚上发你", "text"),
            (103, 6666, "小明", "这次报告要加一页预算表", "text"),
        ]
        for mid, gid, nick, text, kinds in rows:
            st.insert_message({"post_type": "message", "message_type": "group", "group_id": gid,
                               "user_id": 100 + mid, "message_id": mid, "time": now - 7200,
                               "raw_message": text,
                               "message": [{"type": "text", "data": {"text": text}}],
                               "sender": {"nickname": nick}})
        return st

    def _inst(self, app, texts=None, ok=True, error=""):
        inst = app.plugins.plugins["assistant"]["inst"]

        class FakeLLM:
            model = "fake-model"

            def __init__(self):
                self.calls = []
                self.texts = list(texts or ["这是回答 [#M1]"])

            def chat(self, messages, **kw):
                self.calls.append(messages)
                t = self.texts.pop(0) if self.texts else ""
                return fw_llm.LLMResult(ok=ok, text=t, model=self.model,
                                        usage={"prompt_tokens": 123, "completion_tokens": 45},
                                        cost=0.0012, ms=700, error=error)

            def describe(self):
                return {"configured": True, "model": self.model, "fake": True}

        inst._llm = FakeLLM()
        return inst, inst._llm

    # ------------------------------------------------------------ 解析与检索
    def test_scope_parsing(self):
        app = self._app()
        try:
            self._seed()
            inst, _ = self._inst(app)
            s = inst._scope("昨天测试群里谁提到了报告")
            self.assertEqual(s["hours"], 48, s)
            self.assertEqual(s["group"], 6666, s)
            self.assertIn("报告", s["tokens"])
            self.assertNotIn("昨天", s["tokens"], "时间词不该当检索关键词")
            s2 = inst._scope("最近3天有什么待办")
            self.assertEqual(s2["hours"], 72, s2)
            s3 = inst._scope("上周聊了什么")
            self.assertEqual(s3["hours"], 336, s3)
        finally:
            app.stop()

    def test_retrieve_and_citation_lines(self):
        app = self._app()
        try:
            self._seed()
            inst, _ = self._inst(app)
            b = inst._retrieve("报告")
            self.assertTrue(b["messages"], b)
            lines, sources = inst._build("报告", b)
            self.assertTrue(lines[0].startswith("[M1]"), lines[0])
            self.assertEqual(sources[0]["kind"], "M")
            self.assertIn("报告", sources[0]["text"])
            self.assertIn("group_name", sources[0])
            self.assertIn("小明", sources[0]["label"] + sources[0]["text"] + str(sources))
        finally:
            app.stop()

    # ------------------------------------------------------------ 问答
    def test_ask_with_llm_persists_session(self):
        app = self._app()
        try:
            self._seed()
            inst, fake = self._inst(app, texts=["报告由小红完成, 还要加一页预算表 [#M1][#M3]"])
            out = inst.ask("测试群里报告的事怎么样了")
            self.assertTrue(out["ok"], out)
            self.assertIn("小红", out["answer"])
            self.assertTrue(out["sources"])
            self.assertEqual(out["rounds"], 1)
            self.assertEqual(len(fake.calls), 1)
            body = fake.calls[0][1]["content"]
            self.assertIn("[M1]", body, "送进模型的上下文要带来源编号")
            self.assertIn("测试群里报告的事", body)
            sess = core.Store().chat_session(out["session_id"])
            self.assertEqual(len(sess["turns"]), 2, sess["turns"])
            self.assertEqual(sess["turns"][0]["role"], "user")
            self.assertEqual(sess["turns"][1]["role"], "assistant")
            self.assertEqual(sess["tokens_in"], 123)
            self.assertGreater(sess["turns"][1]["sources"][0]["ref"], "")
        finally:
            app.stop()

    def test_ask_degrades_when_model_unavailable(self):
        app = self._app()
        try:
            self._seed()
            inst, _ = self._inst(app, ok=False, error="HTTP 401 无效的密钥")
            out = inst.ask("报告的事")
            self.assertTrue(out["degraded"], out)
            self.assertIn("模型暂时不可用", out["answer"])
            self.assertTrue(out["sources"], "降级时也要把检索到的记录给用户")
            sess = core.Store().chat_session(out["session_id"])
            self.assertEqual(len(sess["turns"]), 2)
            self.assertIn("模型暂时不可用", sess["turns"][1]["content"], "降级回答也要落库")
        finally:
            app.stop()

    def test_ask_reports_when_no_records(self):
        app = self._app()
        try:
            inst, _ = self._inst(app)
            out = inst.ask("有没有关于量子纠缠的讨论", hours=1)
            self.assertTrue(out["ok"])
            self.assertEqual(out["sources"], [])
            self.assertIn("记录里没有找到", out["answer"])
            self.assertTrue(out["degraded"])
        finally:
            app.stop()

    def test_second_round_when_model_asks_for_more(self):
        app = self._app()
        try:
            self._seed()
            inst, fake = self._inst(app, texts=['{"need": {"q": "预算", "hours": 168}}',
                                                "补充后: 预算表要加一页 [#M1]"])
            out = inst.ask("报告还要改什么")
            self.assertEqual(out["rounds"], 2, out)
            self.assertEqual(len(fake.calls), 2)
            self.assertIn("预算表", out["answer"])
            self.assertNotIn('"need"', out["answer"], "不能把 JSON 指令当回答")
        finally:
            app.stop()

    def test_daily_quota(self):
        app = self._app()
        try:
            self._seed()
            inst, _ = self._inst(app)
            ok, errs, saved = app.plugins.set_config("assistant", {"basic": {"daily_quota": 1}})
            self.assertTrue(ok, errs)
            self.assertEqual(saved["basic"]["daily_quota"], 1)
            app.ctx.invalidate_plugin_cfg("assistant")
            inst._llm = inst._llm
            first = inst.ask("报告的事")
            self.assertTrue(first["ok"], first)
            second = inst.ask("报告的事")
            self.assertFalse(second["ok"])
            self.assertIn("额度", second["error"])
        finally:
            app.plugins.set_config("assistant", {"basic": {"daily_quota": 100}})
            app.stop()

    def test_empty_question_and_disabled(self):
        app = self._app()
        try:
            inst, _ = self._inst(app)
            self.assertFalse(inst.ask("   ")["ok"])
            app.plugins.set_config("assistant", {"basic": {"enabled": False}})
            app.ctx.invalidate_plugin_cfg("assistant")
            r = inst.ask("报告的事")
            self.assertFalse(r["ok"])
            self.assertIn("关闭", r["error"])
        finally:
            app.plugins.set_config("assistant", {"basic": {"enabled": True}})
            app.stop()

    def test_routes_contract(self):
        app = self._app(roles=("console",))
        try:
            self._seed()
            inst, _ = self._inst(app, texts=["回答 [#M1]"])
            hit, params = app.router.match("POST", "/api/assistant/ask")
            self.assertTrue(hit)
            req = fw_web.Request(method="POST", path="/api/assistant/ask",
                                 body=json.dumps({"question": "报告"}).encode())
            out = hit.fn(req, app.ctx)
            self.assertTrue(out["ok"], out)
            hit, _ = app.router.match("GET", "/api/assistant/sessions")
            self.assertIn("sessions", hit.fn(fw_web.Request(), app.ctx))
            hit, _ = app.router.match("GET", "/api/assistant/suggest")
            sug = hit.fn(fw_web.Request(), app.ctx)
            self.assertIn("suggestions", sug)
            hit, _ = app.router.match("GET", "/api/assistant/stats")
            st = hit.fn(fw_web.Request(), app.ctx)
            self.assertIn("tokens_in", st)
            self.assertIn("llm", st)
            # 来源详情: 按消息 id 取原文
            mid = core.Store().messages(group="6666", q="报告", hours=24)["rows"][0]["id"]
            hit, params = app.router.match("GET", "/api/assistant/source/M/%d" % mid)
            self.assertTrue(hit, params)
            req = fw_web.Request(method="GET", path="/api/assistant/source/M/%d" % mid,
                                 params={"kind": "M", "ref": str(mid)})
            src = hit.fn(req, app.ctx)
            self.assertTrue(src["ok"], src)
            self.assertIn("报告", src["record"]["raw_message"])
            self.assertIn("ts_text", src["record"])
        finally:
            app.stop()

    def test_source_sid_points_at_the_right_record(self):
        """来源必须能用稳定 sid 定位: 编号(M1/Y1)只在一份上下文里唯一。

        踩过的坑: 点记忆来源直接报"未知来源类型: Y"; 按序号当 id 去查消息会打开
        编号恰好相同的**另一条**消息 —— 所以 sid 优先, 且每种来源都要能取到原文。
        """
        app = self._app()
        try:
            st = self._seed()
            st.memory_set("group:6666", "digest", "群里的共识: 报告要加预算表")
            st.event_upsert({"dedup": "d1", "kind": "deadline", "title": "交实验报告",
                             "group_id": 6666, "due_at": int(time.time()) + 86400})
            st.link_upsert("http://example.com/r", title="报告模板", excerpt="正文摘要")
            mid = st.messages(group="6666", q="预算表", hours=24)["rows"][0]["id"]
            inst, _ = self._inst(app, texts=["看预算表 [#M1] 和长期记忆 [#Y1]"])
            hit, _ = app.router.match("POST", "/api/assistant/ask")
            out = hit.fn(fw_web.Request(method="POST", path="/api/assistant/ask",
                                        body=json.dumps({"question": "报告"}).encode()), app.ctx)
            self.assertTrue(out["ok"], out)
            sids = {s["kind"]: s.get("sid") for s in out["sources"]}
            self.assertTrue(sids.get("M"), sids)      # 消息来源要带稳定定位
            self.assertTrue(sids.get("Y"), sids)      # 记忆来源也要(原先压根没有)
            self.assertTrue(sids.get("M").startswith("m:"))

            def src(kind, ref, sid=""):
                h, _ = app.router.match("GET", "/api/assistant/source/%s/%s" % (kind, ref))
                self.assertTrue(h, kind)
                # query 按**真实 HTTP 形态**构造(parse_qs 出来的是列表) —— 用字符串测会漏掉
                # "str(list)" 这类只在真机上才犯的错
                req = fw_web.Request(method="GET", path="/api/assistant/source/%s/%s" % (kind, ref),
                                     params={"kind": kind, "ref": str(ref)},
                                     query={"sid": [sid]} if sid else {})
                return h.fn(req, app.ctx)

            # 1) 记忆(原先必然失败): 稳定 sid = y:scope|key
            d = src("Y", 1, "y:group:6666|digest")
            self.assertTrue(d["ok"], d)
            self.assertIn("预算表", d["record"]["content"])
            self.assertEqual(d["record"]["scope"], "group:6666")
            self.assertTrue(d["record"]["origin_messages"])     # 给出去核对的消息
            # 2) 序号张冠李戴: ref=1 但 sid 指向真实 id → 必须按 sid 返回
            d = src("M", 1, "m:%d" % mid)
            self.assertTrue(d["ok"], d)
            self.assertIn("预算表", d["record"]["raw_message"])
            # 3) 事件/链接同样支持 sid
            ev = st.events(status="all", limit=5)[0]
            d = src("E", 1, "e:%d" % ev["id"])
            self.assertTrue(d["ok"], d)
            self.assertIn("报告", d["record"]["title"])
            d = src("L", 1, "l:http://example.com/r")
            self.assertTrue(d["ok"], d)
            self.assertEqual(d["record"]["url"], "http://example.com/r")
            # 4) 没有 sid 的老路径: 记忆给不出定位就如实报错, 不是静默返回空原文
            d = src("Y", 1)
            self.assertFalse(d["ok"], d)
            self.assertIn("记忆", d["error"])
        finally:
            app.stop()

    def test_reply_in_chat_private_query(self):
        """在 QQ 私聊里发一句话就能查记录: 允许的人能收到回答, 别人/自己/冷却期不发。

        这条链路横跨采集器 → 管线阶段 → 模型 → NapCat 发送, 之前完全没有, 也是
        "推送的聊天里能不能智能查询"的落地验收。
        """
        app = self._app()
        sent = []
        try:
            self._seed()
            inst, _ = self._inst(app, texts=["小明要交实验报告 [#M1]"])
            # 假 NapCat: 记录发送内容, 不真的连网
            class FakeNap:
                def __init__(self):
                    self.texts, self.forwards = [], []

                def send_text(self, text, user_id=0, group_id=0, max_chars=1200, reply_to=0):
                    self.texts.append({"text": text, "user_id": user_id,
                                       "group_id": group_id, "reply_to": reply_to})
                    return 1

                def send_forward(self, nodes, user_id=0, group_id=0, timeout=40):
                    self.forwards.append({"nodes": nodes, "user_id": user_id, "group_id": group_id})
                    return {"status": "ok", "retcode": 0}

                def send_miniapp_card(self, *a, **kw):
                    return {"status": "failed", "error": "no packet backend"}

            fake = FakeNap()
            real_nap = core.napcat_from_config
            core.napcat_from_config = lambda cfg: fake
            try:
                app.plugins.set_config("assistant", {"reply": {"enable": True, "allow_users": [777],
                                                               "reply_mode": "text", "cooldown": 0,
                                                               "quote": True, "show_sources": True}})
                app.ctx.invalidate_plugin_cfg("assistant")

                def evt(uid, text, **kw):
                    d = {"post_type": "message", "message_type": "private", "user_id": uid,
                         "message_id": 90001, "time": int(time.time()), "raw_message": text,
                         "message": [{"type": "text", "data": {"text": text}}],
                         "sender": {"nickname": "我"}}
                    d.update(kw)
                    return d

                # 1) 白名单内的人私聊提问 → 有回答, 且引用了提问
                r = inst._maybe_reply(evt(777, "谁要交实验报告?"))
                self.assertEqual(r.get("reply"), "sent", r)
                self.assertEqual(len(fake.texts), 1, fake.texts)
                self.assertIn("实验报告", fake.texts[0]["text"])
                self.assertEqual(fake.texts[0]["reply_to"], 90001)
                self.assertEqual(fake.texts[0]["user_id"], 777)
                # 2) 不在白名单 → 一条都不发
                fake.texts.clear()
                r = inst._maybe_reply(evt(888, "谁要交实验报告?"))
                self.assertEqual(r.get("reply"), "skipped(not_allowed)", r)
                self.assertEqual(fake.texts, [])
                # 3) 机器人自己发的消息不回应(否则自己跟自己聊)
                self.assertIsNone(inst._maybe_reply(evt(777, "谁要交实验报告?", self=1)))
                self.assertEqual(fake.texts, [])
                # 4) 冷却期内的第二条被挡
                app.plugins.set_config("assistant", {"reply": {"enable": True, "allow_users": [777], "cooldown": 60, "reply_mode": "text"}})
                app.ctx.invalidate_plugin_cfg("assistant")
                inst._maybe_reply(evt(777, "再问一次"))
                fake.texts.clear()
                r = inst._maybe_reply(evt(777, "又来一条"))
                self.assertEqual(r.get("reply"), "skipped(cooldown)", r)
                self.assertEqual(fake.texts, [])
                # 5) 群里默认不回应; 开了群问答但没带前缀也不回应
                self.assertIsNone(inst._maybe_reply(evt(777, "谁要交报告", message_type="group",
                                                        group_id=6666)))
                app.plugins.set_config("assistant", {"reply": {
                    "enable": True, "allow_users": [777], "cooldown": 0, "reply_mode": "text",
                    "group_enable": True, "groups": [6666]}})
                app.ctx.invalidate_plugin_cfg("assistant")
                self.assertIsNone(inst._maybe_reply(evt(777, "谁要交报告", message_type="group",
                                                        group_id=6666)))
                r = inst._maybe_reply(evt(777, "问 谁要交报告", message_type="group", group_id=6666))
                self.assertEqual(r.get("reply"), "sent", r)
                self.assertEqual(fake.texts[-1]["group_id"], 6666)
                # 6) 别的群即使开了群问答也不理
                self.assertIsNone(inst._maybe_reply(evt(777, "问 谁要交报告", message_type="group",
                                                        group_id=9999)))
                # 7) 合并转发卡片模式: 发的是转发而不是文本
                app.plugins.set_config("assistant", {"reply": {
                    "enable": True, "allow_users": [777], "cooldown": 0,
                    "reply_mode": "forward", "group_enable": True, "groups": [6666]}})
                app.ctx.invalidate_plugin_cfg("assistant")
                fake.texts.clear()
                fake.forwards.clear()
                r = inst._maybe_reply(evt(777, "谁要交实验报告?", message_type="private"))
                self.assertEqual(r.get("reply"), "sent", r)
                self.assertTrue(fake.forwards, "forward 模式应该发合并转发")
                self.assertEqual(fake.texts, [])
                node_text = json.dumps(fake.forwards[0]["nodes"], ensure_ascii=False)
                self.assertIn("提问", node_text)
                self.assertIn("回答", node_text)
                # 8) 统计: 控制台能看到"QQ 里问了几次"
                st = core.state_get("assistant_reply_stats", {}) or {}
                self.assertGreaterEqual(int(st.get("answered") or 0), 2, st)
                self.assertGreaterEqual(int(st.get("skipped") or 0), 2, st)
            finally:
                core.napcat_from_config = real_nap
        finally:
            app.stop()

    def test_llm_from_config_falls_back_to_summarizer(self):
        """llm 段留空时必须回落到 summarizer, 否则会出现"配了总结却问不了"的情况。"""
        cfg = core.Config()
        cfg.set_values({"summarizer": {"base_url": "https://api.deepseek.com/v1",
                                       "api_key": "sk-test", "model": "deepseek-chat"},
                        "llm": {"base_url": "", "api_key": "", "model": ""}})
        llm = fw_llm.from_config(core.Config(), component="t")
        self.assertTrue(llm.configured, llm.describe())
        self.assertEqual(llm.model, "deepseek-chat")
        self.assertEqual(llm.base_url, "https://api.deepseek.com/v1")
        d = llm.describe()
        cfg.set_values({"llm": {"model": "deepseek-v4-pro", "temperature": 0.5}})
        llm2 = fw_llm.from_config(core.Config(), component="t")
        self.assertEqual(llm2.model, "deepseek-v4-pro", llm2.describe())
        self.assertEqual(llm2.temperature, 0.5)
        self.assertEqual(d["configured"], True)

    def test_llm_records_failure_without_raising(self):
        """模型不可用时 LLM 层不能抛异常, 要返回 ok=False 并记账。"""
        bad = fw_llm.LLM(base_url="http://127.0.0.1:1", api_key="x", model="m",
                         timeout=5, retries=1, component="t.bad")
        res = bad.chat([{"role": "user", "content": "hi"}])
        self.assertFalse(res.ok, res)
        self.assertTrue(res.error)
        self.assertEqual(bad.describe()["configured"], True)
        empty = fw_llm.LLM()
        self.assertFalse(empty.configured)
        self.assertFalse(empty.chat([{"role": "user", "content": "hi"}]).ok)


class TestRuntimePower(unittest.TestCase):
    """进程合并 + 功耗档位 + 资源优化。

    真机上踩过的点: 四个 Python 进程各自 ~25MB(≈100MB), 手机上很吃紧;
    合并成一个进程 + 按档位收紧 SQLite 缓存/唤醒间隔, 才能既省内存又省电。
    """

    def set_cfg(self, patch: dict):
        cfg = core.Config()
        cfg.set_values(patch)
        return core.Config()

    def setUp(self):
        # runtime 段每个用例都从默认值开始: 否则上一个用例写的覆盖会串到下一个(真踩过)
        cfg = core.Config()
        cfg.set_values({"runtime": dict(core.DEFAULT_CONFIG["runtime"])})
        core.apply_power_profile(core.Config())

    def test_power_profiles_differ(self):
        seen = {}
        for mode in ("performance", "balanced", "saver"):
            cfg = self.set_cfg({"runtime": {"power_mode": mode}})
            p = core.power_profile(cfg)
            self.assertEqual(p["power_mode"], mode)
            seen[mode] = (p["scheduler_tick"], p["watchdog_interval"], p["console_refresh"],
                          p["sqlite_cache_kb"], p["defer_enrich"], p["log_quiet"])
        self.assertLess(seen["performance"][0], seen["saver"][0], "省电档调度应更疏")
        self.assertLess(seen["performance"][1], seen["saver"][1], "省电档主管间隔应更长")
        self.assertLess(seen["performance"][3], 1000 * 100, seen)
        self.assertGreater(seen["performance"][3], seen["saver"][3], "省电档 SQLite 缓存更小")
        self.assertTrue(seen["saver"][4], "省电档应把富化推到夜间")
        self.assertFalse(seen["balanced"][4])
        self.assertEqual(len(set(seen.values())), 3, "三档必须真的不同: %s" % (seen,))

    def test_power_override_and_invalid_fallback(self):
        cfg = self.set_cfg({"runtime": {"power_mode": "saver", "scheduler_tick": 7,
                                        "defer_enrich": False, "sqlite_cache_kb": 512}})
        p = core.power_profile(cfg)
        self.assertEqual(p["scheduler_tick"], 7, "用户显式配置优先于档位预设")
        self.assertFalse(p["defer_enrich"])
        self.assertEqual(p["sqlite_cache_kb"], 512)
        p2 = core.power_profile(self.set_cfg({"runtime": {"power_mode": "不存在的档"}}))
        self.assertEqual(p2["power_mode"], "balanced", "非法档位应回退 balanced")
        self.assertTrue(p2["all_profiles"])

    def test_resolve_mode_auto_by_platform(self):
        cfg = core.Config()
        cfg.set_values({"runtime": {"mode": "auto"}})
        self.assertEqual(core.resolve_mode(core.Config(), android=True), "single",
                         "手机上 auto 应解析为单进程(省内存)")
        self.assertEqual(core.resolve_mode(core.Config(), android=False), "multi")
        cfg.set_values({"runtime": {"mode": "multi"}})
        self.assertEqual(core.resolve_mode(core.Config(), android=True), "multi", "显式配置优先")
        cfg.set_values({"runtime": {"mode": "乱写"}})
        self.assertIn(core.resolve_mode(core.Config(), android=False), ("single", "multi"))

    def test_enrich_window(self):
        cfg = self.set_cfg({"runtime": {"power_mode": "saver",
                                        "enrich_window": "02:00-06:00"}})
        c = core.Config()
        p = core.power_profile(c)
        self.assertTrue(p["defer_enrich"])
        import datetime
        def ts(h, m=0):
            d = datetime.datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
            return d.timestamp()
        self.assertTrue(core.should_enrich_now(c, ts(3)), "夜间窗口内应允许富化")
        self.assertFalse(core.should_enrich_now(c, ts(12)), "白天应跳过富化")
        # 跨零点窗口
        c2 = self.set_cfg({"runtime": {"enrich_window": "23:00-05:00"}})
        self.assertTrue(core.should_enrich_now(c2, ts(1)))
        self.assertTrue(core.should_enrich_now(c2, ts(23, 30)))
        self.assertFalse(core.should_enrich_now(c2, ts(12)))
        # 非省电档永远允许
        c3 = self.set_cfg({"runtime": {"power_mode": "performance"}})
        self.assertTrue(core.should_enrich_now(c3, ts(12)))

    def test_sqlite_cache_follows_profile(self):
        core.apply_power_profile(self.set_cfg({"runtime": {"power_mode": "saver"}}))
        con = core.Store().conn()
        try:
            self.assertEqual(int(con.execute("PRAGMA cache_size").fetchone()[0]), -1000)
            self.assertGreater(int(con.execute("PRAGMA busy_timeout").fetchone()[0]), 0)
        finally:
            con.close()
        core.apply_power_profile(self.set_cfg({"runtime": {"power_mode": "performance"}}))
        con = core.Store().conn()
        try:
            self.assertEqual(int(con.execute("PRAGMA cache_size").fetchone()[0]), -8000)
        finally:
            con.close()
        core.apply_power_profile(core.Config())

    def test_instance_status_shape(self):
        st = core.instance_status(core.Config())
        for k in ("mode", "running_mode", "pid", "alive", "services", "power", "beats"):
            self.assertIn(k, st)
        for n in ("collector", "scheduler", "web", "watchdog"):
            self.assertIn(n, st["services"])
        self.assertIn(st["mode"], ("single", "multi"))

    def test_lazy_imports_keep_memory_small(self):
        """控制台/调度进程不该顺带拉起 summarizer(它连带 media/linkfetch/extract/render)。"""
        code = ("import sys; import qqops.webapp, qqops.scheduler; "
                "print('summarizer' in str([m for m in sys.modules if 'summarizer' in m]))")
        p = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                           env={**os.environ, "QQOPS_DATA": TMP},
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=90)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        self.assertIn("False", p.stdout, "webapp/scheduler 顶部不该导入 summarizer: %s" % p.stdout)

    def test_runner_supervision_decisions(self):
        """主管的判定逻辑: 线程死了→重启; 反复退出→熔断; 退避期内→等待。"""
        runner = runner_mod.Runner(cfg=core.Config(), services=("collector", "web"))
        calls = []
        runner._spawn = lambda name: (calls.append(name), True)[1]

        class Dead:
            def is_alive(self):
                return False

        runner.threads["collector"] = Dead()
        runner.threads["web"] = Dead()
        res1 = dict((n, (s, m)) for n, s, m in runner.supervise_once())
        self.assertEqual(res1["collector"][0], "restarted", res1)
        self.assertIn("collector", calls)
        # 立刻再判一次: 同一线程仍"死", 但退避时间没到 → 等待, 不无限重启
        runner.threads["collector"] = Dead()
        res2 = dict((n, (s, m)) for n, s, m in runner.supervise_once())
        self.assertEqual(res2["collector"][0], "waiting", res2)
        # 窗口内失败到阈值 → 熔断
        for _ in range(6):
            runner.backoff.fail("collector")
        runner.threads["collector"] = Dead()
        res3 = dict((n, (s, m)) for n, s, m in runner.supervise_once())
        self.assertEqual(res3["collector"][0], "crash_loop", res3)
        self.assertIn("watchdog", res3)
        self.assertEqual(res3["watchdog"][0], "ok")

    def test_single_process_instance_end_to_end(self):
        """单进程模式: 一个进程同时提供采集口与控制台, 状态里四个服务共享同一 PID。"""
        cfg = core.Config()
        col_port, web_port = free_port(), free_port()
        cfg.set_values({"collector": {"port": col_port}, "web": {"port": web_port},
                        "runtime": {"mode": "single", "power_mode": "saver"}})
        core.apply_power_profile(core.Config())
        msg = core.start_instance(core.Config(), wait=45, mode="single")
        try:
            self.assertIn("已启动", msg, msg)
            rt = core.runtime_state()
            self.assertEqual(rt["mode"], "single")
            self.assertTrue(rt["alive"], rt)
            pid = int(rt["pid"])
            # 四个服务共享同一个 PID(这就是"合并进程"的证据)
            st = core.service_status(core.Config())
            for n in ("collector", "scheduler", "web", "watchdog"):
                self.assertEqual(int(st[n]["pid"]), pid, "%s 应属于同一个进程: %s" % (n, st[n]))
                self.assertEqual(st[n]["mode"], "single")
                self.assertTrue(st[n]["running"], "%s 应为运行中: %s" % (n, st[n]))
            if POSIX:
                self.assertGreater(st["collector"]["rss"], 0)
            # 健康检查走单进程分支: 端口/心跳
            self.assertTrue(core.service_health("collector", core.Config(), timeout=6)["ok"],
                            "单进程模式下采集口应健康(端口应答)")
            self.assertTrue(core.service_health("web", core.Config(), timeout=6)["ok"],
                            "单进程模式下控制台端口应健康")
            time.sleep(1)
            self.assertTrue(core.service_health("scheduler", core.Config(), timeout=10)["ok"],
                            "单进程模式下调度线程应有新鲜心跳")
            wd = core.service_health("watchdog", core.Config(), timeout=10)
            self.assertTrue(wd["ok"], "单进程模式下主管(看门狗角色)应有心跳 | %s | %s"
                            % (wd, ci_note(
                                "beats=%s\nruntime=%s\nrunner log 末尾:\n%s"
                                % (core.beats(), core.runtime_state(),
                                   (core._run(["tail", "-n", "20",
                                               os.path.join(core.DATA, "logs", "runner.log")], 5)
                                    if os.name != "nt" else "")[:1200]),
                                "看门狗心跳缺失")))
            # 端到端: 往采集口发一条消息, 必须落库
            ev = {"post_type": "message", "message_type": "group", "group_id": 7777,
                  "user_id": 5, "message_id": 700001, "time": int(time.time()),
                  "raw_message": "单进程模式端到端测试",
                  "message": [{"type": "text", "data": {"text": "单进程模式端到端测试"}}],
                  "sender": {"nickname": "单进程"}}
            req = urllib.request.Request("http://127.0.0.1:%d/" % col_port,
                                         data=json.dumps(ev, ensure_ascii=False).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.status, 200)
            rows = core.Store().messages(group="7777", q="单进程模式端到端", hours=1)["rows"]
            self.assertEqual(len(rows), 1, "单进程模式下采集口仍要能落库")
            # 控制台接口可用(同一进程内)
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % web_port, timeout=10) as r:
                self.assertEqual(r.status, 200)
        finally:
            stopped = core.stop_instance(core.Config())
            self.assertIn("停止", stopped, stopped)
        self.assertFalse(core.runtime_state()["alive"], "停止后运行清单必须失效")
        self.assertFalse(core.port_open(col_port), "采集口应已释放")


if __name__ == "__main__":
    unittest.main(verbosity=2)
