#!/usr/bin/env python3
"""kernel_counters.sh runs against a stub `sysctl` that prints the first fixture on its first call and the second one on its
second call. No driver is needed, and none is touched."""
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/kernel_counters.sh"
ACCEL = ROOT / "kexts/NVRM/accel/nvrm-accel.cpp"

# Real counters, a counter this script has never heard of, a string sysctl and an unrelated one.
BEFORE = """kern.ostype: Darwin
debug.nvaccel_vm_alloc: 1183
debug.nvaccel_vm_dealloc: 1132
debug.nvaccel_vm_refused: 385
debug.nvaccel_iop_flips: 100
debug.nvaccel_brand_new_counter: 5
debug.nvrmfb_vram_grants: 10
debug.nvrm_winwhy: head 0: some text 42
"""
AFTER = BEFORE.replace("vm_alloc: 1183", "vm_alloc: 1283").replace("vm_refused: 385", "vm_refused: 405").replace(
    "brand_new_counter: 5", "brand_new_counter: 17")


def write_exe(path, body):
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


STUBS = SHARED = None


def setUpModule():
    """The stubs and the script copy are built once: the first run of every new file is slow on a Mac."""
    global STUBS, SHARED
    SHARED = pathlib.Path(tempfile.mkdtemp())
    STUBS = SHARED / "bin"
    STUBS.mkdir()
    # A copy outside the checkout, so the meanings in kexts/ do not leak into tests that are not about them.
    (SHARED / "copy/tools").mkdir(parents=True)
    shutil.copy(SCRIPT, SHARED / "copy/tools/kernel_counters.sh")
    write_exe(STUBS / "sysctl", 'd=$STUB_STATE; echo "$*" >> "$d/args"\n'
              'n=$(( $(cat "$d/count" 2>/dev/null || echo 0) + 1 )); echo $n > "$d/count"\n'
              'f="$d/out.$n"; [ -f "$f" ] || f=$(ls "$d"/out.* | tail -1); cat "$f"')
    write_exe(STUBS / "sleep", "exit 0")
    write_exe(STUBS / "sw_vers", 'case $1 in -productVersion) echo 26.0;; *) echo 25A1;; esac')
    write_exe(STUBS / "kextstat", "echo '  186    3 0xffffff7f8a0 0x1000 0x1000 com.nullmoth.NVRM (1.15.0) AAAA-BBBB <5 4>'")


def tearDownModule():
    shutil.rmtree(SHARED)


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())  # per-test: fixtures, the call count and the sysctl arguments
        self.addCleanup(shutil.rmtree, self.dir)

    def fixtures(self, *outputs):
        for i, text in enumerate(outputs, 1):
            (self.dir / f"out.{i}").write_text(text)

    def run_script(self, *args, script=None):
        env = dict(os.environ, PATH=f"{STUBS}:/usr/bin:/bin", STUB_STATE=str(self.dir))
        script = script or SHARED / "copy/tools/kernel_counters.sh"
        return subprocess.run(["/bin/bash", str(script), *args], capture_output=True, text=True, timeout=60, env=env)

    @staticmethod
    def rows(out):
        """name -> columns after the name, for the table rows (lines that start with two spaces and debug.)."""
        return {l.split()[0]: l.split()[1:] for l in out.splitlines() if l.startswith("  debug.")}


class DeltaTable(Case):
    def test_changed_counters_with_delta_and_rate(self):
        self.fixtures(BEFORE, AFTER)
        r = self.run_script("-s", "4")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = self.rows(r.stdout)
        self.assertEqual(rows["debug.nvaccel_vm_alloc"][:4], ["1183", "1283", "+100", "25.0"])
        self.assertEqual(rows["debug.nvaccel_vm_refused"][:4], ["385", "405", "+20", "5.0"])
        self.assertEqual(rows["debug.nvaccel_brand_new_counter"][:4], ["5", "17", "+12", "3.0"])
        self.assertEqual(len(rows), 3)

    def test_unchanged_summary_and_header(self):
        self.fixtures(BEFORE, AFTER)
        out = self.run_script("-s", "4").stdout
        self.assertIn("\nunchanged: 3 counters\n", out)
        self.assertIn("macOS   26.0 (25A1)", out)
        self.assertIn("Kexts   com.nullmoth.NVRM 1.15.0", out)
        self.assertIn("elapsed: 4 s", out)

    def test_grouped_by_prefix_and_sysctl_called_twice(self):
        self.fixtures(BEFORE, AFTER)
        out = self.run_script("-s", "4").stdout
        self.assertEqual(re.findall(r"^(nv\w+)$", out, re.M), ["nvaccel_brand", "nvaccel_vm"])
        self.assertEqual((self.dir / "args").read_text().split("\n")[:2], ["-a", "-a"])
        self.assertEqual((self.dir / "count").read_text().strip(), "2")

    def test_negative_delta_and_a_counter_that_appears(self):
        self.fixtures("debug.nvrmfb_vram_mapped_bytes: 900\n",
                      "debug.nvrmfb_vram_mapped_bytes: 600\ndebug.nvrmfb_vram_grants: 3\n")
        rows = self.rows(self.run_script("-s", "3").stdout)
        self.assertEqual(rows["debug.nvrmfb_vram_mapped_bytes"][:4], ["900", "600", "-300", "-100.0"])
        self.assertEqual(rows["debug.nvrmfb_vram_grants"][:4], ["-", "3", "-", "-"])

    def test_nothing_changed(self):
        self.fixtures(BEFORE)
        out = self.run_script("-s", "1").stdout
        self.assertEqual(self.rows(out), {})
        self.assertIn("unchanged: 6 counters", out)


class CommandMode(Case):
    def test_exit_status_passes_through_and_table_is_still_printed(self):
        self.fixtures(BEFORE, AFTER)
        r = self.run_script("--", "/bin/sh", "-c", "echo ran; exit 7")
        self.assertEqual(r.returncode, 7)
        self.assertIn("ran\n", r.stdout)
        self.assertIn("debug.nvaccel_vm_alloc", r.stdout)

    def test_success_is_zero(self):
        self.fixtures(BEFORE, AFTER)
        self.assertEqual(self.run_script("--", "/usr/bin/true").returncode, 0)

    def test_rate_is_delta_over_the_measured_time(self):
        self.fixtures(BEFORE, AFTER)
        out = self.run_script("--", "/bin/sleep", "1").stdout
        elapsed = int(re.search(r"elapsed: (\d+) s", out).group(1))
        self.assertGreaterEqual(elapsed, 1)
        rate = self.rows(out)["debug.nvaccel_vm_alloc"][3]
        self.assertEqual(rate, f"{100 / elapsed:.1f}")

    def test_no_rate_when_no_whole_second_passed(self):
        self.fixtures(BEFORE, AFTER)
        out = self.run_script("--", "/usr/bin/true").stdout
        if "elapsed: 0 s" in out:  # a command that crosses a second boundary legitimately has elapsed 1
            self.assertEqual(self.rows(out)["debug.nvaccel_vm_alloc"][2:4], ["+100", "-"])

    def test_missing_command_is_127(self):
        self.fixtures(BEFORE, AFTER)
        self.assertEqual(self.run_script("--", "/nonexistent/cmd").returncode, 127)


class NotLoaded(Case):
    MSG = "the NullMoth kernel extensions are not loaded (no debug.nvaccel* / debug.nvrmfb* sysctls)"

    def test_exit_3_with_message_in_every_mode(self):
        self.fixtures("kern.ostype: Darwin\n")
        for args in (["-s", "1"], ["--", "/usr/bin/true"], ["-a"], ["-l"]):
            r = self.run_script(*args)
            self.assertEqual(r.returncode, 3, args)
            self.assertIn(self.MSG, r.stderr)

    def test_command_is_not_run_when_not_loaded(self):
        self.fixtures("kern.ostype: Darwin\n")
        marker = self.dir / "ran"
        self.run_script("--", "/usr/bin/touch", str(marker))
        self.assertFalse(marker.exists())

    def test_unloaded_during_the_run(self):
        self.fixtures(BEFORE, "kern.ostype: Darwin\n")
        self.assertEqual(self.run_script("-s", "1").returncode, 3)


class Names(Case):
    def test_names_are_discovered(self):
        self.fixtures(BEFORE)
        names = self.run_script("-l").stdout.split()
        self.assertIn("debug.nvaccel_brand_new_counter", names)
        self.assertIn("debug.nvrmfb_vram_grants", names)
        self.assertNotIn("debug.nvrm_winwhy", names)  # a string dump, not a counter
        self.assertNotIn("kern.ostype", names)

    def test_all_prints_every_value_once(self):
        self.fixtures(BEFORE)
        r = self.run_script("-a")
        self.assertEqual(r.returncode, 0)
        rows = self.rows(r.stdout)
        self.assertEqual(rows["debug.nvaccel_vm_alloc"], ["1183"])
        self.assertEqual(len(rows), 6)
        self.assertEqual((self.dir / "count").read_text().strip(), "1")


class Meanings(Case):
    def test_annotated_from_the_source_next_to_the_script(self):
        self.fixtures(BEFORE, AFTER)
        tree = SHARED / "annotated"
        (tree / "tools").mkdir(parents=True, exist_ok=True)
        (tree / "kexts/NVRM/accel").mkdir(parents=True, exist_ok=True)
        shutil.copy(SCRIPT, tree / "tools")
        (tree / "kexts/NVRM/accel/nvrm-accel.cpp").write_text(
            '#define NVACCEL_CNT(nm, var, desc) SYSCTL_QUAD(_debug, OID_AUTO, nm, 0, (SInt64 *)&var, desc)\n'
            'NVACCEL_CNT(nvaccel_vm_alloc,         gCntVmAlloc,        "NVVidMemory::allocPhysical successes");\n'
            'NVACCEL_CNT(nvaccel_iop_ok_h0,        gCntIopOkHead[0],   "frames copied to head 0\'s panel");\n')
        out = self.run_script("-s", "4", script=tree / "tools/kernel_counters.sh").stdout
        line = [l for l in out.splitlines() if "debug.nvaccel_vm_alloc" in l][0]
        self.assertTrue(line.endswith("  # NVVidMemory::allocPhysical successes"), line)
        refused = [l for l in out.splitlines() if "debug.nvaccel_vm_refused" in l][0]
        self.assertNotIn("#", refused)

    def test_plain_names_without_the_source(self):
        self.fixtures(BEFORE, AFTER)
        out = self.run_script("-s", "4").stdout
        self.assertNotIn("#", out)
        self.assertIn("debug.nvaccel_vm_alloc", out)

    def test_every_description_in_the_real_source_is_picked_up(self):
        want = dict(re.findall(r'^NVACCEL_CNT\((\w+),\s*[^,]+,\s*"(.*)"\);', ACCEL.read_text(), re.M))
        self.assertGreater(len(want), 20)
        self.fixtures("".join(f"debug.{n}: 1\n" for n in want))
        out = self.run_script("-a", script=SCRIPT).stdout
        for name, desc in want.items():
            line = [l for l in out.splitlines() if l.startswith(f"  debug.{name} ")]
            self.assertEqual(len(line), 1, name)
            self.assertTrue(line[0].endswith(f"  # {desc}"), line[0])


class Usage(Case):
    def test_bad_arguments(self):
        self.fixtures(BEFORE)
        for args in ([], ["-s"], ["-s", "x"], ["-s", "2", "--", "true"], ["-a", "--", "true"], ["--"], ["-z"]):
            self.assertEqual(self.run_script(*args).returncode, 2, args)


if __name__ == "__main__":
    unittest.main()
