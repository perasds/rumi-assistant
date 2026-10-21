"""Temporary local input adapter for the diagnostic probe.

The normal frontend remains Vue 3 + Vant. This owner-only Tk window performs
no authentication or network requests and never logs or persists its inputs.
Importing this module does not open a window.
"""

import tkinter as tk


class LocalCredentialInputError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def read_local_credentials() -> tuple[str, str]:
    """Copy submitted inputs into RAM, then clear and destroy the owned window."""
    root = account = password = None
    result = None
    failed = False
    try:
        root = tk.Tk()
        root.title("本机临时诊断输入")
        root.resizable(False, False)
        account, password = tk.StringVar(root), tk.StringVar(root)
        reveal = tk.BooleanVar(root, value=False)
        tk.Label(root, text="仅供本机诊断输入；账号密码不写入日志或报告。").pack(padx=16, pady=12)
        tk.Label(root, text="游戏账号").pack()
        account_entry = tk.Entry(root, textvariable=account, show="", width=38)
        account_entry.pack(padx=16, pady=4)
        tk.Label(root, text="游戏密码").pack()
        password_entry = tk.Entry(root, textvariable=password, show="*", width=38)
        password_entry.pack(padx=16, pady=4)
        status = tk.Label(root, text="")
        status.pack()

        def toggle():
            password_entry.config(show="" if reveal.get() else "*")

        def submit():
            nonlocal result
            values = (account.get(), password.get())
            if not all(values):
                status.config(text="请填写账号和密码。")
                return
            result = values
            root.quit()

        def callback_failed(*unused):
            nonlocal failed
            failed = True
            root.quit()

        # Tk normally prints callback tracebacks. Keep any UI error local.
        root.report_callback_exception = callback_failed
        tk.Checkbutton(root, text="显示密码（仅本机）", variable=reveal, command=toggle).pack()
        tk.Button(root, text="确认提交", command=submit).pack(pady=6)
        tk.Button(root, text="取消", command=root.quit).pack(pady=6)
        root.protocol("WM_DELETE_WINDOW", root.quit)
        account_entry.focus_set()
        root.mainloop()
    except Exception:
        failed = True
    finally:
        for value in (account, password):
            if value is not None:
                try:
                    value.set("")
                except Exception:
                    failed = True
        if root is not None:
            try:
                root.destroy()
            except Exception:
                failed = True
    if failed:
        raise LocalCredentialInputError("PROBE_CREDENTIAL_INPUT_UNAVAILABLE")
    if result is None:
        raise LocalCredentialInputError("PROBE_CREDENTIAL_INPUT_CANCELLED")
    return result
