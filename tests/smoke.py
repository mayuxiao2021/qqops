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
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
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

from qqops import (collector, core, extract, fileread, linkfetch, media, render,  # noqa: E402
                   summarizer, svc, tools, watchdog, webapp)

POSIX = os.name != "nt"


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
    def setUp(self):
        self.store = core.Store()

    def ev(self, gid=1001, text="hello", t=None, nick="tester"):
        return {"time": t or core.now(), "post_type": "message", "message_type": "group",
                "group_id": gid, "user_id": 2002, "message_id": 33,
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
            p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
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
            if core.proc_net_ok():
                self.assertEqual(conf["pid"], os.getpid(), conf)
                # 模块名命中自身命令行 → 认得出"自己人"
                self.assertTrue(core.check_port_free(port, "smoke")["ours"])
            else:
                # Android: /proc/net/tcp 被禁, 只能如实说"定位不到", 不能瞎猜
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

    def _spawn_listener(self, port: int, tag: str, data_dir: str = None):
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
        return p

    def test_reclaim_kills_own_zombie(self):
        """占用者是我们自己的僵死进程 → 自动回收并给出提示。"""
        if not owner_identifiable():
            self.skipTest("本机既读不了 /proc/net/tcp 也没有 pgrep, 无法认定自己人")
        port = free_port()
        fake = "qqops.fakecollector"
        p = self._spawn_listener(port, fake, core.DATA)
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
        p2 = self._spawn_listener(port2, fake, core.DATA)
        try:
            self.assertTrue(core.check_port_free(port2, fake)["ours"])
        finally:
            p2.kill()
            p2.wait(timeout=10)

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
        core.state_set("watchdog_beat", 0)
        p = subprocess.Popen([sys.executable, "-m", "qqops.watchdog"], cwd=ROOT,
                             env=self._env(), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        try:
            h = core.service_health("watchdog", timeout=20)
            self.assertTrue(h["ok"], h)
            self.assertGreater(int(core.state_get("watchdog_beat", 0) or 0), 0)
        finally:
            p.kill()
            p.wait(timeout=10)

    def _env(self):
        env = dict(os.environ)
        env["QQOPS_DATA"] = TMP
        env["TZ"] = "Asia/Shanghai"
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
            path = m.build(out_dir, verbose=False)
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
        return env

    def test_collector_roundtrip_and_shutdown(self):
        port = free_port()
        proc = subprocess.Popen([sys.executable, "-m", "qqops.collector", "--port", str(port)],
                                cwd=ROOT, env=self._env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        try:
            if not wait_port(port):
                proc.kill()
                out = (proc.stdout.read() if proc.stdout else "")[-1500:]
                self.fail("采集器未在超时内监听, 输出:\n%s" % out)
            ev = {"post_type": "message", "message_type": "group", "group_id": 4242,
                  "user_id": 7, "message_id": 1, "time": int(time.time()),
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
                                stderr=subprocess.STDOUT, text=True)
        try:
            if not wait_port(port):
                proc.kill()
                out = (proc.stdout.read() if proc.stdout else "")[-1500:]
                self.fail("控制台未在超时内监听, 输出:\n%s" % out)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
