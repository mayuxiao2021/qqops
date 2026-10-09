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

from qqops import collector, core, summarizer, svc, tools, webapp  # noqa: E402

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
        self.assertEqual(set(core.SERVICE_MODULES), {"collector", "scheduler", "web"})
        self.assertTrue(callable(svc.main))


class TestConsistency(unittest.TestCase):
    def test_version_matches_everywhere(self):
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), core.VERSION)
        import qqops
        self.assertEqual(qqops.__version__, core.VERSION)
        self.assertEqual(core.DEFAULT_CONFIG["version"], core.VERSION)

    def test_stdlib_only(self):
        """第三方依赖必须为 0: 只有标准库 + 包内相对导入。"""
        std = set(sys.stdlib_module_names)
        outside = set()
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "qqops")):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                src = open(os.path.join(dirpath, fn), encoding="utf-8").read()
                for node in ast.walk(ast.parse(src)):
                    if isinstance(node, ast.Import):
                        outside.update(a.name.split(".")[0] for a in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        outside.add(node.module.split(".")[0])
        bad = sorted(m for m in outside if m not in std and m != "qqops")
        self.assertEqual(bad, [], "出现第三方依赖: %s" % bad)

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
                               capture_output=True, text=True, timeout=90)
            self.assertIn(p.returncode, (0, 1), "命令失败: %s\n%s" % (args, p.stdout[-500:]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
