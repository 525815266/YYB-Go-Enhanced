import contextlib
import importlib.util
import io
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPTS = {
    "pcmgr": load("pcmgr_script", "腾讯电脑管家_code版.py"),
    "jd": load("jd_script", "京东CK采集_code版.py"),
}


class WebsiteAppIDScriptsAreOffByDefault(unittest.TestCase):
    """A website-application AppID taken through the mini-program login has been
    seen to cost the account its authorization, so the two shipped scripts that use
    one do nothing unless asked, whatever version of the service they talk to."""

    def run_main(self, module, allow):
        env = {"YYB_ALLOW_WEBSITE_APPID": allow} if allow else {}
        out = io.StringIO()
        with patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(out):
            if not allow:
                os.environ.pop("YYB_ALLOW_WEBSITE_APPID", None)
            module.main()
        return out.getvalue()

    def test_default_run_stops_before_any_request(self):
        for name, module in SCRIPTS.items():
            with self.subTest(script=name):
                with patch.object(module, "SERVERS", ["127.0.0.1:1"]), patch.object(module, "run_account", Mock(side_effect=KeyboardInterrupt)):
                    printed = self.run_main(module, "")
                self.assertIn("YYB_ALLOW_WEBSITE_APPID=1", printed)
                self.assertIn("YYB_CODE_ALLOW_APPIDS", printed)

    def test_anything_but_one_stays_stopped(self):
        for name, module in SCRIPTS.items():
            for value in ("0", "true", "yes", " "):
                with self.subTest(script=name, value=value):
                    with patch.object(module, "SERVERS", ["127.0.0.1:1"]), patch.object(module, "run_account", Mock(side_effect=KeyboardInterrupt)):
                        self.run_main(module, value)

    def test_explicit_opt_in_reaches_the_account_run(self):
        for name, module in SCRIPTS.items():
            with self.subTest(script=name):
                run_account = Mock(side_effect=KeyboardInterrupt)
                with patch.object(module, "SERVERS", ["127.0.0.1:1"]), patch.object(module, "run_account", run_account):
                    with self.assertRaises(KeyboardInterrupt):
                        self.run_main(module, "1")
                run_account.assert_called_once()


if __name__ == "__main__":
    unittest.main()
