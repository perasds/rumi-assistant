import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/local_credential_form.py"


class Var:
    def __init__(self, root, value="", *, boolean=False):
        self.value = value
        (root.booleans if boolean else root.strings).append(self)

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Widget:
    def __init__(self, root, kind, **options):
        self.root, self.kind, self.options = root, kind, options
        root.widgets.append(self)

    def pack(self, **options):
        pass

    def config(self, **options):
        self.options.update(options)

    def focus_set(self):
        self.root.focused = self


class Root:
    def __init__(self, action):
        self.action = action
        self.strings, self.booleans, self.widgets = [], [], []
        self.protocols = {}
        self.destroyed = self.quitted = False

    def title(self, title):
        self.window_title = title

    def resizable(self, *args):
        pass

    def protocol(self, name, callback):
        self.protocols[name] = callback

    def quit(self):
        self.quitted = True

    def destroy(self):
        self.destroyed = True

    def mainloop(self):
        self.action(self)

    def button(self, text):
        return next(w for w in self.widgets if w.kind == "Button" and w.options["text"] == text)

    def entries(self):
        return [w for w in self.widgets if w.kind == "Entry"]


def load_form(monkeypatch, action):
    root = Root(action)
    calls = []
    def create_root():
        calls.append(root)
        return root
    fake = SimpleNamespace(Tk=create_root,
        StringVar=lambda owner: Var(owner), BooleanVar=lambda owner, value=False: Var(owner, value, boolean=True))
    for kind in ("Label", "Entry", "Checkbutton", "Button"):
        setattr(fake, kind, lambda owner, _kind=kind, **options: Widget(owner, _kind, **options))
    monkeypatch.setitem(sys.modules, "tkinter", fake)
    assert SCRIPT.is_file(), "local credential form implementation is missing"
    spec = importlib.util.spec_from_file_location("local_credential_form_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert calls == []  # Importing the helper must not open a real or fake window.
    return module, root


def fill(root):
    root.strings[0].set("synthetic-account")
    root.strings[1].set(" synthetic-password ")


def test_mask_default_local_reveal_exact_input_copy_cleanup_and_no_output(monkeypatch, capsys):
    def action(root):
        account, password = root.entries()
        assert account.options.get("show", "") == ""
        assert password.options["show"] == "*"
        fill(root)
        toggle = next(w for w in root.widgets if w.kind == "Checkbutton")
        root.booleans[0].set(True)
        toggle.options["command"]()
        assert password.options["show"] == ""
        assert root.strings[1].get() == " synthetic-password "
        root.booleans[0].set(False)
        toggle.options["command"]()
        assert password.options["show"] == "*"
        root.button("确认提交").options["command"]()
    module, root = load_form(monkeypatch, action)
    assert module.read_local_credentials() == ("synthetic-account", " synthetic-password ")
    assert root.quitted and root.destroyed
    assert [value.get() for value in root.strings] == ["", ""]
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("exit_way", ["button", "window", "loop_exit"])
def test_cancel_clears_fields_destroys_owner_and_raises_fixed_error(monkeypatch, capsys, exit_way):
    def action(root):
        fill(root)
        if exit_way == "button":
            root.button("取消").options["command"]()
        elif exit_way == "window":
            root.protocols["WM_DELETE_WINDOW"]()
    module, root = load_form(monkeypatch, action)
    with pytest.raises(module.LocalCredentialInputError) as caught:
        module.read_local_credentials()
    assert caught.value.code == "PROBE_CREDENTIAL_INPUT_CANCELLED"
    assert "synthetic" not in str(caught.value)
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert root.destroyed and all(value.get() == "" for value in root.strings)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_empty_submit_stays_local_until_both_values_are_entered(monkeypatch):
    def action(root):
        root.button("确认提交").options["command"]()
        assert not root.quitted
        fill(root)
        root.button("确认提交").options["command"]()
    module, root = load_form(monkeypatch, action)
    assert module.read_local_credentials() == ("synthetic-account", " synthetic-password ")
    assert root.destroyed


@pytest.mark.parametrize("failure", ["loop", "callback"])
def test_gui_failure_cannot_print_exception_or_leave_input_values(monkeypatch, capsys, failure):
    def action(root):
        fill(root)
        if failure == "loop":
            raise ValueError("synthetic-password")
        root.report_callback_exception(ValueError, ValueError("synthetic-password"), None)
    module, root = load_form(monkeypatch, action)
    with pytest.raises(module.LocalCredentialInputError) as caught:
        module.read_local_credentials()
    assert caught.value.code == "PROBE_CREDENTIAL_INPUT_UNAVAILABLE"
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert root.destroyed and all(value.get() == "" for value in root.strings)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
