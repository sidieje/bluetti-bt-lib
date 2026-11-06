"""Regression tests for the command line switches (audit finding M4).

``type=bool`` made argparse hand back ``bool(value)``, so every value was
truthy: ``-e False`` meant encryption, and ``--on False`` switched the field
*on*.  The switches are ``store_true`` now, which also means ``--on``/``--off``
no longer demand a value that used to be parsed and thrown away.
"""

import io
import sys
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bluetti_bt_lib.scripts import bluetti_read, bluetti_readall, bluetti_write

MAC = "00:11:22:33:44:55"


class ScriptCase(unittest.TestCase):
    """Runs a script's ``start()`` without touching Bluetooth."""

    def run_start(self, module, entrypoint, argv):
        entrypoint_mock = MagicMock()
        # The scripts only use asyncio.run(), so a stub keeps the module global
        # local to the patch instead of shadowing asyncio for the whole process.
        stub = SimpleNamespace(run=MagicMock())
        out = io.StringIO()
        with (
            patch.object(sys, "argv", argv),
            patch.object(module, entrypoint, entrypoint_mock),
            patch.object(module, "asyncio", stub),
            redirect_stdout(out),
        ):
            module.start()
        return entrypoint_mock, out.getvalue()


class TestWriteArgs(ScriptCase):
    def run_write(self, extra):
        return self.run_start(
            bluetti_write,
            "async_write",
            ["bluetti-write", "-m", MAC, "-t", "EB3A", *extra],
        )

    def test_on_is_a_flag(self):
        write, _ = self.run_write(["--on", "ctrl_ac"])
        write.assert_called_once_with(MAC, "EB3A", False, "ctrl_ac", True)

    def test_off_is_a_flag(self):
        write, _ = self.run_write(["--off", "ctrl_ac"])
        write.assert_called_once_with(MAC, "EB3A", False, "ctrl_ac", False)

    def test_on_no_longer_accepts_a_value(self):
        # "False" used to be parsed as True, so asking for off got on.
        with self.assertRaises(SystemExit):
            self.run_write(["--on", "False", "ctrl_ac"])

    def test_on_and_off_conflict(self):
        with self.assertRaises(SystemExit):
            self.run_write(["--on", "--off", "ctrl_ac"])

    def test_no_value_prints_help(self):
        write, out = self.run_start(
            bluetti_write,
            "async_write",
            ["bluetti-write", "-m", MAC, "-t", "EB3A", "ctrl_ac"],
        )
        write.assert_not_called()
        self.assertIn("usage:", out)

    def test_encryption_defaults_to_false_for_unencrypted_devices(self):
        write, _ = self.run_write(["--on", "ctrl_ac"])
        self.assertIs(write.call_args.args[2], False)

    def test_encryption_flag_sets_true(self):
        write, _ = self.run_write(["-e", "--on", "ctrl_ac"])
        self.assertIs(write.call_args.args[2], True)

    def test_encryption_no_longer_accepts_a_value(self):
        with self.assertRaises(SystemExit):
            self.run_write(["-e", "False", "--on", "ctrl_ac"])

    def test_numeric_value_still_wins_over_off(self):
        write, _ = self.run_write(["--off", "-v", "5", "ctrl_dc"])
        self.assertEqual(write.call_args.args[4], 5)

    def test_select_still_wins_over_on(self):
        write, _ = self.run_write(["--on", "-s", "STANDARD", "ctrl_charging_mode"])
        self.assertEqual(write.call_args.args[4], "STANDARD")


class TestReadArgs(ScriptCase):
    def test_encryption_defaults_to_false(self):
        read, _ = self.run_start(
            bluetti_read,
            "async_read_device",
            ["bluetti-read", "-m", MAC, "-t", "EB3A"],
        )
        read.assert_called_once_with(MAC, "EB3A", False)

    def test_encryption_flag_sets_true(self):
        read, _ = self.run_start(
            bluetti_read,
            "async_read_device",
            ["bluetti-read", "-m", MAC, "-t", "EB3A", "-e"],
        )
        read.assert_called_once_with(MAC, "EB3A", True)

    def test_encryption_no_longer_accepts_a_value(self):
        with self.assertRaises(SystemExit):
            self.run_start(
                bluetti_read,
                "async_read_device",
                ["bluetti-read", "-m", MAC, "-t", "EB3A", "-e", "False"],
            )


class TestReadallArgs(ScriptCase):
    def test_encryption_defaults_to_false(self):
        read, _ = self.run_start(
            bluetti_readall,
            "async_read_device",
            ["bluetti-readall", "-m", MAC, "-v", "1"],
        )
        read.assert_called_once_with(MAC, 1, False)

    def test_encryption_flag_sets_true(self):
        read, _ = self.run_start(
            bluetti_readall,
            "async_read_device",
            ["bluetti-readall", "-m", MAC, "-v", "1", "-e"],
        )
        read.assert_called_once_with(MAC, 1, True)

    def test_encryption_no_longer_accepts_a_value(self):
        with self.assertRaises(SystemExit):
            self.run_start(
                bluetti_readall,
                "async_read_device",
                ["bluetti-readall", "-m", MAC, "-v", "1", "-e", "False"],
            )


if __name__ == "__main__":
    unittest.main()
