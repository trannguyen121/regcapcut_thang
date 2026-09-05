import queue
import re
import unittest

from modules.ui.reg_capcut_tab import RegCapCutWriter
from tk_ui import Writer


class LogTimestampTests(unittest.TestCase):
    def assert_writer_timestamps_complete_lines_once(self, writer, output_queue):
        written = []
        writer.write_file = written.append

        # print() normally sends the content and newline separately.
        writer.write("first log")
        self.assertEqual(written, [])
        writer.write("\nsecond")
        writer.write(" log\n")

        self.assertEqual(len(written), 2)
        pattern = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] (.+)\n$")
        self.assertEqual([pattern.match(line).group(1) for line in written], ["first log", "second log"])
        self.assertEqual(output_queue.get_nowait(), written[0])
        self.assertEqual(output_queue.get_nowait(), written[1])
        self.assertTrue(output_queue.empty())

    def test_standalone_capcut_log_has_time(self):
        output = queue.Queue()
        self.assert_writer_timestamps_complete_lines_once(RegCapCutWriter(output), output)

    def test_combined_ui_log_has_time(self):
        output = queue.Queue()
        self.assert_writer_timestamps_complete_lines_once(Writer(output), output)

    def test_blank_lines_stay_blank(self):
        self.assertEqual(RegCapCutWriter.timestamp_line("\n"), "\n")
        self.assertEqual(Writer.timestamp_line("\n"), "\n")


if __name__ == "__main__":
    unittest.main()
