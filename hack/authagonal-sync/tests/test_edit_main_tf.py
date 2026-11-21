"""edit-main-tf: only the value of the lines whose local name was given changes, found by name and never by line number."""
import os
import tempfile

from .support import MAIN_TF, SHA_A, SHA_B, SHA_C, WorldCase

NEW_CTL = "ghcr.io/authagonal/ingress-nginx/controller:v9.9.9-0123456789ab@" + SHA_C
NEW_CG = "ghcr.io/authagonal/ingress-nginx/kube-webhook-certgen:v9.9.9-0123456789ab-go9.9.9@sha256:" + "d" * 64


class EditMainTfTest(WorldCase):
    def edit(self, text, *args):
        p = os.path.join(self._tmp, "main.tf")
        with open(p, "w", newline="") as f:
            f.write(text)
        r = self.w.sync("edit-main-tf", "--file", p, *args)
        with open(p, newline="") as f:
            return r, f.read()

    @staticmethod
    def diff(a, b):
        x, y = a.split("\n"), b.split("\n")
        assert len(x) == len(y), "line count changed"
        return [(m, n) for m, n in zip(x, y) if m != n]

    def tf(self):
        return MAIN_TF % {"suffix": "d825f68c4461", "ca": SHA_A, "cg": SHA_B}

    def test_controller_only_changes_one_line(self):
        r, new = self.edit(self.tf(), "--controller", NEW_CTL)
        self.assertEqual(r.returncode, 0, r.stderr)
        ((old, now),) = self.diff(self.tf(), new)
        self.assertIn("ingress_nginx_controller_ref = ", old)
        self.assertEqual(now, f'  ingress_nginx_controller_ref = "{NEW_CTL}"   # pinned by digest')

    def test_certgen_given_changes_a_second_line_and_keeps_the_alignment(self):
        r, new = self.edit(self.tf(), "--controller", NEW_CTL, "--certgen", NEW_CG)
        self.assertEqual(r.returncode, 0, r.stderr)
        changed = self.diff(self.tf(), new)
        self.assertEqual(len(changed), 2)
        self.assertEqual(changed[1][1], f'  ingress_nginx_certgen_ref    = "{NEW_CG}"')

    def test_lines_are_found_by_name_not_by_number(self):
        r, new = self.edit("\n" * 7 + self.tf(), "--controller", NEW_CTL, "--certgen", NEW_CG)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.diff("\n" * 7 + self.tf(), new)), 2)
        self.assertIn(NEW_CTL, new)

    def test_lines_that_only_mention_the_name_are_left_alone(self):
        r, new = self.edit(self.tf(), "--controller", NEW_CTL, "--certgen", NEW_CG)
        for ln in new.split("\n"):
            if "ingress_nginx_controller_parts" in ln or "ingress_nginx_certgen_parts" in ln:
                self.assertIn("local.ingress_nginx_", ln)
        self.assertEqual(sum(1 for ln in new.split("\n") if "local.ingress_nginx_controller_ref" in ln), 1)

    def test_line_endings_and_a_missing_final_newline_are_kept(self):
        crlf = self.tf().replace("\n", "\r\n").rstrip("\r\n")
        r, new = self.edit(crlf, "--controller", NEW_CTL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(new.replace(NEW_CTL, "X").count("\r\n"), crlf.count("\r\n"))
        self.assertFalse(new.endswith("\n"))

    def test_a_ref_that_is_not_digest_pinned_is_refused_and_the_file_is_untouched(self):
        for bad in ("ghcr.io/authagonal/ingress-nginx/controller:v1", "", NEW_CTL.split("@")[0] + "@sha256:abc"):
            r, new = self.edit(self.tf(), "--controller", bad)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertEqual(new, self.tf())

    def test_a_missing_or_doubled_line_is_refused_and_the_file_is_untouched(self):
        no_certgen = "\n".join(ln for ln in self.tf().split("\n") if not ln.lstrip().startswith("ingress_nginx_certgen_ref"))
        doubled = self.tf() + 'locals {\n  ingress_nginx_controller_ref = "x"\n}\n'
        for text, args in ((no_certgen, ["--controller", NEW_CTL, "--certgen", NEW_CG]), (doubled, ["--controller", NEW_CTL])):
            r, new = self.edit(text, *args)
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(new, text)


if __name__ == "__main__":
    import unittest
    unittest.main()
