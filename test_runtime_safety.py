from __future__ import annotations

import unittest

import runtime_safety


class _Broken:
    def write(self, text):  # noqa: ANN001
        raise BrokenPipeError("pipe closed")

    def flush(self):
        raise BrokenPipeError("pipe closed")


class SafeStreamTests(unittest.TestCase):
    def test_write_after_pipe_close_is_swallowed(self):
        stream = runtime_safety.SafeStream(_Broken(), "stdout")
        self.assertEqual(stream.write("hello"), 5)
        stream.flush()
        self.assertTrue(stream.broken)
        self.assertEqual(stream.write("again"), 5)

    def test_exit_code_description(self):
        self.assertIn("未捕获异常", runtime_safety.describe_exit_code(4294967295))
        self.assertIn("未捕获异常", runtime_safety.describe_exit_code(-1))
        self.assertEqual(runtime_safety.describe_exit_code(0), "正常退出")


if __name__ == "__main__":
    unittest.main()
