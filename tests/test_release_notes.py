import unittest

from tools.release_notes import extract_version_notes, render_release_notes


class ReleaseNotesTests(unittest.TestCase):
    def test_extracts_exact_version_section(self) -> None:
        changelog = """# 更新日志

## v1.2.3 - 2026-09-29

- 修复登录问题。
- 增加账号管理。

## v1.2.2 - 2026-09-28

- 旧内容。
"""
        notes = extract_version_notes(changelog, "1.2.3", "full")
        self.assertIn("修复登录问题", notes)
        self.assertNotIn("旧内容", notes)

    def test_magisk_can_use_named_section(self) -> None:
        changelog = """# 更新日志

## Magisk v0.3.0 - 2026-09-29

- 修复模块启动。
"""
        self.assertIn("修复模块启动", extract_version_notes(changelog, "0.3.0", "magisk"))

    def test_magisk_can_fall_back_to_matching_bullet(self) -> None:
        changelog = """# 更新日志

## 2026-08-17

- 发布 [Magisk v0.1.4](https://example.invalid)。
"""
        self.assertIn("Magisk v0.1.4", extract_version_notes(changelog, "0.1.4", "magisk"))

    def test_missing_version_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "缺少发布章节"):
            extract_version_notes("# 更新日志\n", "9.9.9", "full")

    def test_rendered_full_release_contains_changes_and_asset_help(self) -> None:
        notes = render_release_notes(
            version="1.2.3",
            tag="v1.2.3",
            kind="full",
            repository="owner/repo",
            changes="- 修复登录问题。",
        )
        self.assertIn("## 本次更新", notes)
        self.assertIn("修复登录问题", notes)
        self.assertIn("yyb-go-v1.2.3-<平台>-<架构>", notes)
        self.assertIn("checksums.txt", notes)
        self.assertIn("owner/repo/blob/v1.2.3/CHANGELOG.md", notes)


if __name__ == "__main__":
    unittest.main()
