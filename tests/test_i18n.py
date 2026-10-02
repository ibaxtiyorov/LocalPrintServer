"""Every key used in code/templates exists in EN, RU and UZ with the same placeholders."""
import json
import re
from pathlib import Path

import pytest

from lps import catalog
from lps import jobs as J
from lps.auth import ROLES
from lps.settings_service import SPEC

ROOT = Path(__file__).resolve().parent.parent / "lps"
TABLES = {code: json.loads((ROOT / "translations" / f"{code}.json").read_text(encoding="utf-8"))
          for code in ("en", "ru", "uz")}


def used_keys() -> set[str]:
    keys = set()
    for p in ROOT.rglob("*"):
        if p.suffix in (".html", ".js", ".py"):
            s = p.read_text(encoding="utf-8")
            keys |= set(re.findall(r"""\bt\(\s*['"]([a-z][\w.]*[\w])['"]""", s))
            keys |= set(re.findall(r"""['"]((?:err|msg|status\.reason)\.[a-z_][\w.]*)['"]""", s))
    keys = {k for k in keys if not k.endswith((".", "_"))}
    keys |= {f"state.{s}" for s in J.ALL_STATES}
    keys |= {f"status.{s}" for s in ("READY", "IDLE", "PRINTING", "BUSY", "WAITING", "PAUSED",
                                     "UNAVAILABLE", "ERROR", "SPOOLER_ERROR")}
    keys |= {f"media.{m.key}" for m in catalog.MEDIA_TYPES}
    keys |= {f"quality.{q}" for q in catalog.QUALITY}
    keys |= {f"color.{c}" for c in catalog.COLOR}
    keys |= {f"orientation.{o}" for o in catalog.ORIENTATION_CHOICES}
    keys |= {f"scaling.{s}" for s in catalog.SCALING_CHOICES}
    keys |= {f"role.{r}" for r in ROLES}
    keys |= {f"set.{k}" for k in SPEC}
    keys |= {f"set.login_mode.{m}" for m in ("PASSWORD", "USERNAME", "GUEST")}
    keys |= {f"office.engine.{e}" for e in ("msoffice", "libreoffice", "simulated")}
    keys |= {f"office.reason.{r}" for r in ("disabled", "no_word", "no_excel", "no_libreoffice",
                                            "none_installed")}
    keys |= {"print.file_meta_converted"}
    keys |= {"set.copies_mode.DRIVER", "set.copies_mode.APPLICATION",
             "js.cancel_cancelled", "js.cancel_cancelling", "js.cancel_final"}
    return keys


@pytest.mark.parametrize("lang", ["en", "ru", "uz"])
def test_all_used_keys_translated(lang):
    missing = sorted(k for k in used_keys() if k not in TABLES[lang] and k != "sim.control")
    assert not missing, missing


def test_same_key_sets():
    assert set(TABLES["ru"]) == set(TABLES["en"])
    assert set(TABLES["uz"]) == set(TABLES["en"])


def test_placeholders_match():
    ph = re.compile(r"\{(\w+)\}")
    for key, text in TABLES["en"].items():
        for lang in ("ru", "uz"):
            assert set(ph.findall(TABLES[lang][key])) == set(ph.findall(text)), (lang, key)


def test_uzbek_is_latin():
    cyr = re.compile(r"[Ѐ-ӿ]")
    assert not [k for k, v in TABLES["uz"].items() if cyr.search(v)]
