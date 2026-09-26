from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Sequence
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup, Tag

NON_SUBMITTED_INPUT_TYPES = frozenset({"file", "reset", "button"})
CHECKABLE_TYPES = frozenset({"checkbox", "radio"})
SUBMIT_INPUT_TYPES = frozenset({"submit", "image"})

Pair = tuple[str, str]


class FormError(ValueError):
    pass


@dataclass(frozen=True)
class FormState:
    action: str
    method: str
    fields: tuple[Pair, ...]
    submitters: Mapping[str, tuple[Pair, ...]] = field(default_factory=lambda: MappingProxyType({}))

    def get(self, name: str) -> str | None:
        return next((v for n, v in self.fields if n == name), None)

    def get_all(self, name: str) -> list[str]:
        return [v for n, v in self.fields if n == name]

    def names(self) -> list[str]:
        return [n for n, _ in self.fields]

    def with_values(self, overrides: Mapping[str, str | Sequence[str] | None]) -> FormState:
        fields = list(self.fields)
        for name, value in overrides.items():
            if value is None:
                new_pairs: list[Pair] = []
            elif isinstance(value, str):
                new_pairs = [(name, value)]
            else:
                new_pairs = [(name, v) for v in value]
            positions = [i for i, (n, _) in enumerate(fields) if n == name]
            insert_at = positions[0] if positions else len(fields)
            fields = [p for p in fields if p[0] != name]
            fields[insert_at:insert_at] = new_pairs
        return FormState(self.action, self.method, tuple(fields), self.submitters)

    def submit_with(self, name: str) -> FormState:
        if name not in self.submitters:
            raise FormError(f"form has no submit control named {name!r}")
        return FormState(self.action, self.method, self.fields + self.submitters[name], self.submitters)

    def postback(self, event_target: str, event_argument: str = "") -> FormState:
        return self.with_values({"__EVENTTARGET": event_target, "__EVENTARGUMENT": event_argument})

    def encode(self) -> str:
        return urlencode(list(self.fields))


def find_form(soup: BeautifulSoup, form_id: str | None = None) -> Tag:
    if form_id is not None:
        form = soup.find("form", id=form_id)
        if form is None:
            raise FormError(f"no form with id {form_id!r}")
        return form
    forms = soup.find_all("form")
    if len(forms) != 1:
        raise FormError(f"expected exactly one form, found {len(forms)}")
    return forms[0]


def extract_form_state(html: str | BeautifulSoup, base_url: str, form_id: str | None = None) -> FormState:
    soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "lxml")
    form = find_form(soup, form_id)
    action = urljoin(base_url, (form.get("action") or "").strip() or base_url)
    method = (form.get("method") or "get").strip().upper()
    if method not in {"GET", "POST"}:
        raise FormError(f"unsupported form method {method!r}")

    fields: list[Pair] = []
    submitters: dict[str, tuple[Pair, ...]] = {}
    for control in form.find_all(["input", "select", "textarea", "button"]):
        name = control.get("name")
        if not name or _is_disabled(control):
            continue
        if control.name == "input":
            kind = (control.get("type") or "text").strip().lower()
            if kind in SUBMIT_INPUT_TYPES:
                submitters[name] = _submitter_pairs(name, kind, control.get("value"))
            elif kind in CHECKABLE_TYPES:
                if control.has_attr("checked"):
                    fields.append((name, control.get("value", "on")))
            elif kind not in NON_SUBMITTED_INPUT_TYPES:
                fields.append((name, control.get("value", "")))
        elif control.name == "select":
            fields.extend((name, v) for v in _selected_values(control))
        elif control.name == "textarea":
            fields.append((name, _normalize_newlines(control.get_text())))
        elif control.name == "button":
            if (control.get("type") or "submit").strip().lower() == "submit":
                submitters[name] = ((name, control.get("value", "")),)
    return FormState(action, method, tuple(fields), MappingProxyType(submitters))


def _submitter_pairs(name: str, kind: str, value: str | None) -> tuple[Pair, ...]:
    if kind == "image":
        return ((f"{name}.x", "0"), (f"{name}.y", "0"))
    return ((name, value if value is not None else "Submit"),)


def _is_disabled(control: Tag) -> bool:
    if control.has_attr("disabled"):
        return True
    for fieldset in control.find_parents("fieldset"):
        if fieldset.has_attr("disabled"):
            legend = fieldset.find("legend", recursive=False)
            if legend is None or not any(p is legend for p in control.parents):
                return True
    return False


def _option_value(option: Tag) -> str:
    if option.has_attr("value"):
        return option["value"]
    return " ".join(option.get_text().split())


def _option_disabled(option: Tag) -> bool:
    parent = option.parent
    return option.has_attr("disabled") or (
        parent is not None and parent.name == "optgroup" and parent.has_attr("disabled")
    )


def _selected_values(select: Tag) -> list[str]:
    options = select.find_all("option")
    enabled = [o for o in options if not _option_disabled(o)]
    selected = [o for o in enabled if o.has_attr("selected")]
    if select.has_attr("multiple"):
        return [_option_value(o) for o in selected]
    if selected:
        return [_option_value(selected[-1])]
    return [_option_value(enabled[0])] if enabled else []


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


_JS_STRING = r"""(['"])((?:\\.|(?!\1).)*)\1"""
_DO_POSTBACK = re.compile(r"__doPostBack\(\s*" + _JS_STRING + r"\s*,\s*" + _JS_STRING.replace("\\1", "\\3") + r"\s*\)")
_POSTBACK_OPTIONS = re.compile(
    r"WebForm_PostBackOptions\(\s*" + _JS_STRING + r"\s*,\s*" + _JS_STRING.replace("\\1", "\\3")
)


def parse_postback(script: str) -> tuple[str, str] | None:
    for pattern in (_DO_POSTBACK, _POSTBACK_OPTIONS):
        match = pattern.search(script)
        if match:
            return _unescape_js(match.group(2)), _unescape_js(match.group(4))
    return None


def _unescape_js(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)
