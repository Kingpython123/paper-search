#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""选刊工具的图形界面。

和原项目最大的差别是：抓取跑在独立线程里，界面不会假死。

原项目把 selenium 爬取直接放在 tkinter 按钮回调里，
所以作者只能在按钮上写「运行期间程序会假死」、让用户去看终端输出，
想停下来还得 Ctrl+C 掉整个终端。

这里的做法是标准的三件套：
    worker 线程跑 Pipeline
    -> 通过 queue.Queue 把日志/结果回传
    -> 主线程用 root.after(100) 轮询队列刷新界面
线程只设置 stop 标志，由 Pipeline 在每个耗时步骤前检查，实现可中断。
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from journal_picker import __version__
from journal_picker.cache import Cache
from journal_picker.models import SearchQuery
from journal_picker.pipeline import Pipeline, SORT_KEYS
from journal_picker.report import write_all

APP_TITLE = f"期刊选择助手 v{__version__} —— 按研究方向反查候选投稿期刊"

SCOPE_LABELS = {
    "仅标题（最精准，推荐）": "title",
    "标题 + 摘要": "abstract",
    "全文（结果多但噪声大）": "fulltext",
}
SORT_LABELS = {v: k for k, v in SORT_KEYS.items()}
BACKEND_LABELS = {
    "直连（能上 Google 就用这个）": "http",
    "SerpApi（需 API key，最稳）": "serpapi",
    "浏览器（可手动过验证码）": "selenium",
}


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title(APP_TITLE)
        root.geometry("1080x760")
        root.minsize(900, 640)

        self.queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.result = None
        self.out_files: list[Path] = []

        self._build_widgets()
        self.root.after(100, self._drain_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ 界面

    def _build_widgets(self) -> None:
        pad = {"padx": 6, "pady": 3}
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # ---------- 检索条件 ----------
        top = ttk.LabelFrame(root, text="检索条件")
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        for c in (1, 3):
            top.columnconfigure(c, weight=1)

        ttk.Label(top, text="必须全含的词").grid(row=0, column=0, sticky="w", **pad)
        self.e_all = ttk.Entry(top)
        self.e_all.grid(row=0, column=1, columnspan=3, sticky="ew", **pad)
        self.e_all.insert(0, "video captioning")
        ttk.Label(top, text="多个词用逗号分隔，含空格的短语会按精确短语检索",
                  foreground="#888").grid(row=1, column=1, columnspan=3, sticky="w", padx=6)

        ttk.Label(top, text="含任一即可的词").grid(row=2, column=0, sticky="w", **pad)
        self.e_any = ttk.Entry(top)
        self.e_any.grid(row=2, column=1, columnspan=3, sticky="ew", **pad)
        ttk.Label(top, text="选填，逗号分隔，相互之间是 OR 关系",
                  foreground="#888").grid(row=3, column=1, columnspan=3, sticky="w", padx=6)

        ttk.Label(top, text="检索范围").grid(row=4, column=0, sticky="w", **pad)
        self.cb_scope = ttk.Combobox(top, values=list(SCOPE_LABELS), state="readonly",
                                     width=26)
        self.cb_scope.current(0)
        self.cb_scope.grid(row=4, column=1, sticky="w", **pad)

        ttk.Label(top, text="年份范围").grid(row=4, column=2, sticky="e", **pad)
        yf = ttk.Frame(top)
        yf.grid(row=4, column=3, sticky="w", **pad)
        self.e_from = ttk.Entry(yf, width=7)
        self.e_from.insert(0, "2021")
        self.e_from.pack(side="left")
        ttk.Label(yf, text=" 到 ").pack(side="left")
        self.e_to = ttk.Entry(yf, width=7)
        self.e_to.pack(side="left")
        ttk.Label(yf, text="  留空表示不限", foreground="#888").pack(side="left")

        ttk.Label(top, text="最多分析论文").grid(row=5, column=0, sticky="w", **pad)
        self.e_max = ttk.Entry(top, width=10)
        self.e_max.insert(0, "200")
        self.e_max.grid(row=5, column=1, sticky="w", **pad)

        ttk.Label(top, text="排序方式").grid(row=5, column=2, sticky="e", **pad)
        self.cb_sort = ttk.Combobox(top, values=list(SORT_LABELS), state="readonly",
                                    width=34)
        self.cb_sort.current(list(SORT_LABELS.values()).index("papers"))
        self.cb_sort.grid(row=5, column=3, sticky="w", **pad)

        # ---------- 数据源 ----------
        src = ttk.LabelFrame(root, text="数据源与输出")
        src.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        src.columnconfigure(3, weight=1)

        self.v_openalex = tk.BooleanVar(value=True)
        self.v_scholar = tk.BooleanVar(value=False)
        self.v_crossref = tk.BooleanVar(value=True)
        # letpub 默认开启：分区/IF/审稿周期是选刊最常看的几项，
        # 慢和偶尔限流的代价换来的信息量更值，用户想快速试探再手动关掉。
        self.v_letpub = tk.BooleanVar(value=True)
        # CCF 匹配是纯本地字符串匹配，不发请求，默认开着几乎不影响耗时。
        self.v_ccf = tk.BooleanVar(value=True)
        self.v_journal = tk.BooleanVar(value=True)
        self.v_conf = tk.BooleanVar(value=True)

        ttk.Checkbutton(src, text="OpenAlex（主力，免费无需配置）",
                        variable=self.v_openalex).grid(row=0, column=0, sticky="w", **pad)
        ttk.Checkbutton(src, text="Google 学术", variable=self.v_scholar,
                        command=self._toggle_scholar).grid(row=0, column=1, sticky="w", **pad)
        ttk.Checkbutton(src, text="Crossref 兜底补载体",
                        variable=self.v_crossref).grid(row=0, column=2, sticky="w", **pad)
        ttk.Checkbutton(src, text="letpub 补分区/IF/审稿周期（默认开，慢）",
                        variable=self.v_letpub).grid(row=0, column=3, sticky="w", **pad)
        ttk.Checkbutton(src, text="CCF 目录匹配（本地，不联网，默认开）",
                        variable=self.v_ccf).grid(row=0, column=4, sticky="w", **pad)

        ttk.Label(src, text="Google 学术后端").grid(row=1, column=0, sticky="w", **pad)
        self.cb_backend = ttk.Combobox(src, values=list(BACKEND_LABELS),
                                       state="disabled", width=28)
        self.cb_backend.current(0)
        self.cb_backend.grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(src, text="SerpApi key").grid(row=1, column=2, sticky="e", **pad)
        self.e_serpapi = ttk.Entry(src, state="disabled", show="*")
        self.e_serpapi.grid(row=1, column=3, sticky="ew", **pad)

        ttk.Label(src, text="纳入载体").grid(row=2, column=0, sticky="w", **pad)
        tf = ttk.Frame(src)
        tf.grid(row=2, column=1, sticky="w", **pad)
        ttk.Checkbutton(tf, text="期刊", variable=self.v_journal).pack(side="left")
        ttk.Checkbutton(tf, text="会议", variable=self.v_conf).pack(side="left", padx=(8, 0))

        ttk.Label(src, text="邮箱（选填）").grid(row=2, column=2, sticky="e", **pad)
        self.e_mail = ttk.Entry(src)
        self.e_mail.grid(row=2, column=3, sticky="ew", **pad)

        ttk.Label(src, text="输出目录").grid(row=3, column=0, sticky="w", **pad)
        of = ttk.Frame(src)
        of.grid(row=3, column=1, columnspan=3, sticky="ew", **pad)
        of.columnconfigure(0, weight=1)
        self.e_out = ttk.Entry(of)
        self.e_out.insert(0, str(Path.cwd() / "output"))
        self.e_out.grid(row=0, column=0, sticky="ew")
        ttk.Button(of, text="浏览…", command=self._choose_dir,
                   width=8).grid(row=0, column=1, padx=(6, 0))

        # ---------- 结果表 ----------
        mid = ttk.LabelFrame(root, text="候选期刊（双击一行打开期刊主页）")
        mid.grid(row=2, column=0, sticky="nsew", padx=10, pady=4)
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)

        cols = ("idx", "name", "type", "ccf", "n", "cite", "q", "if", "yr2", "review", "accept")
        headers = ("#", "刊名 / 会议", "类型", "CCF", "命中", "被引", "分区",
                   "IF", "两年均被引", "审稿/会议周期", "录用")
        widths = (40, 370, 50, 44, 50, 60, 50, 60, 80, 110, 60)
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=12)
        for col, head, width in zip(cols, headers, widths):
            self.tree.heading(col, text=head,
                              command=lambda c=col: self._sort_tree(c))
            anchor = "w" if col == "name" else ("center" if col in ("type", "ccf") else "e")
            self.tree.column(col, width=width, anchor=anchor,
                             stretch=(col == "name"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        vs.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.bind("<Double-1>", self._open_row)

        # ---------- 日志 ----------
        low = ttk.LabelFrame(root, text="运行日志")
        low.grid(row=3, column=0, sticky="ew", padx=10, pady=4)
        low.columnconfigure(0, weight=1)
        self.txt = tk.Text(low, height=8, wrap="word", state="disabled",
                           font=("Consolas", 9))
        self.txt.grid(row=0, column=0, sticky="ew")
        ls = ttk.Scrollbar(low, orient="vertical", command=self.txt.yview)
        ls.grid(row=0, column=1, sticky="ns")
        self.txt.configure(yscrollcommand=ls.set)

        # ---------- 操作栏 ----------
        bar = ttk.Frame(root)
        bar.grid(row=4, column=0, sticky="ew", padx=10, pady=(4, 10))
        bar.columnconfigure(4, weight=1)
        self.btn_run = ttk.Button(bar, text="开始检索", command=self._start)
        self.btn_run.grid(row=0, column=0)
        self.btn_stop = ttk.Button(bar, text="停止", command=self._stop,
                                   state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=6)
        self.btn_open = ttk.Button(bar, text="打开结果目录", command=self._open_dir,
                                   state="disabled")
        self.btn_open.grid(row=0, column=2)
        self.btn_html = ttk.Button(bar, text="在浏览器查看", command=self._open_html,
                                   state="disabled")
        self.btn_html.grid(row=0, column=3, padx=6)
        self.progress = ttk.Progressbar(bar, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=4, sticky="ew", padx=8)
        self.lbl_status = ttk.Label(bar, text="就绪")
        self.lbl_status.grid(row=0, column=5)

    def _toggle_scholar(self) -> None:
        state = "readonly" if self.v_scholar.get() else "disabled"
        self.cb_backend.configure(state=state)
        self.e_serpapi.configure(state="normal" if self.v_scholar.get() else "disabled")

    def _choose_dir(self) -> None:
        path = filedialog.askdirectory(initialdir=self.e_out.get() or os.getcwd())
        if path:
            self.e_out.delete(0, "end")
            self.e_out.insert(0, path)

    # ------------------------------------------------------------------ 运行

    def _read_query(self) -> SearchQuery | None:
        def split(text: str) -> list[str]:
            for sep in ("，", ";", "；"):
                text = text.replace(sep, ",")
            return [t.strip() for t in text.split(",") if t.strip()]

        alls = split(self.e_all.get())
        anys = split(self.e_any.get())
        if not alls and not anys:
            messagebox.showwarning("缺少检索词", "至少填一个关键词。")
            return None

        providers = []
        if self.v_openalex.get():
            providers.append("openalex")
        if self.v_scholar.get():
            providers.append("scholar")
        if not providers:
            messagebox.showwarning("缺少数据源", "至少勾选一个数据源。")
            return None

        types = []
        if self.v_journal.get():
            types.append("journal")
        if self.v_conf.get():
            types.append("conference")
        if not types:
            messagebox.showwarning("缺少载体类型", "期刊和会议至少勾一个。")
            return None

        def as_int(entry: ttk.Entry, name: str, default=None):
            raw = entry.get().strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                messagebox.showwarning("格式错误", f"{name} 需要填整数，当前是「{raw}」。")
                raise ValueError(name)

        try:
            year_from = as_int(self.e_from, "起始年份")
            year_to = as_int(self.e_to, "截止年份")
            max_papers = as_int(self.e_max, "最多分析论文", 200) or 200
        except ValueError:
            return None

        if year_from and year_to and year_from > year_to:
            messagebox.showwarning("年份反了", "起始年份不能大于截止年份。")
            return None

        return SearchQuery(
            keywords_all=alls, keywords_any=anys,
            scope=SCOPE_LABELS[self.cb_scope.get()],
            year_from=year_from, year_to=year_to,
            max_papers=max(1, max_papers),
            providers=providers, include_types=types,
        )

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        query = self._read_query()
        if query is None:
            return

        outdir = self.e_out.get().strip() or str(Path.cwd() / "output")
        backend = BACKEND_LABELS.get(self.cb_backend.get(), "http")
        serpapi = self.e_serpapi.get().strip() or None
        if backend == "serpapi" and not serpapi:
            messagebox.showwarning("缺少 key", "选了 SerpApi 后端就得填 API key。")
            return

        self.tree.delete(*self.tree.get_children())
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")
        self.out_files = []
        self.result = None
        self.stop_flag.clear()

        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_open.configure(state="disabled")
        self.btn_html.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.lbl_status.configure(text="检索中…")

        opts = dict(
            mailto=self.e_mail.get().strip() or None,
            scholar_backend=backend, serpapi_key=serpapi,
            use_crossref=self.v_crossref.get(), use_letpub=self.v_letpub.get(),
            use_ccf=self.v_ccf.get(),
            sort_key=SORT_LABELS[self.cb_sort.get()], outdir=outdir,
        )
        # daemon=True：用户直接关窗口时进程不会被工作线程卡住
        self.worker = threading.Thread(target=self._work, args=(query, opts),
                                       daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        self.stop_flag.set()
        self.lbl_status.configure(text="正在停止…")
        self._log("收到停止指令，正在收尾（已抓到的结果仍会输出）")

    # -------------------------------------------------- 工作线程（不碰任何控件）

    def _work(self, query: SearchQuery, opts: dict) -> None:
        cache = None
        pipe = None
        try:
            cache = Cache()
            pipe = Pipeline(
                cache=cache, mailto=opts["mailto"],
                scholar_backend=opts["scholar_backend"],
                serpapi_key=opts["serpapi_key"],
                use_crossref=opts["use_crossref"],
                use_letpub=opts["use_letpub"],
                use_ccf=opts["use_ccf"],
                on_progress=lambda msg: self.queue.put(("log", msg)),
                stop=self.stop_flag.is_set,
            )
            self.queue.put(("log", f"检索式：{query.describe()}"))
            result = pipe.run(query, sort_key=opts["sort_key"])
            self.queue.put(("result", result))

            if result.journals:
                files = write_all(result, opts["outdir"])
                self.queue.put(("files", files))
            else:
                self.queue.put(("log", "没有命中任何期刊，试试放宽检索范围或年份。"))
        except Exception as exc:
            self.queue.put(("log", "运行出错：\n" + traceback.format_exc()))
            self.queue.put(("error", str(exc)))
        finally:
            if pipe is not None:
                pipe.close()
            if cache is not None:
                cache.close()
            self.queue.put(("done", None))

    # ---------------------------------------------- 主线程：轮询队列刷新界面

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "result":
                    self.result = payload
                    self._fill_tree(payload)
                elif kind == "files":
                    self.out_files = list(payload)  # type: ignore[arg-type]
                    self._log("已生成：" + "、".join(p.name for p in self.out_files))
                    self.btn_open.configure(state="normal")
                    if any(p.suffix == ".html" for p in self.out_files):
                        self.btn_html.configure(state="normal")
                elif kind == "error":
                    messagebox.showerror("出错了", str(payload))
                elif kind == "done":
                    self._finish()
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _finish(self) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.btn_run.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        if self.result is not None and getattr(self.result, "journals", None):
            s = self.result.stats
            self.lbl_status.configure(
                text=f"完成：{s.papers_after_merge} 篇 / {s.journals} 个载体 / {s.elapsed:.0f}s")
            for w in s.warnings:
                self._log("提示：" + w)
        else:
            self.lbl_status.configure(text="已结束")

    def _log(self, msg: str) -> None:
        self.txt.configure(state="normal")
        self.txt.insert("end", msg.rstrip() + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _fill_tree(self, result) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, j in enumerate(result.journals, 1):
            m = j.metrics
            # 会议展示固定周期参考值，期刊展示 letpub 统计周期；
            # 两种数字性质不同，不能混在一列里让人误以为是同一种统计口径。
            if j.venue_type == "conference" and m.conf_cycle_months is not None:
                cycle = f"约{m.conf_cycle_months:g}个月(固定)"
            else:
                cycle = m.review_period or ""
            self.tree.insert("", "end", iid=str(i - 1), values=(
                i,
                j.name,
                {"journal": "期刊", "conference": "会议"}.get(j.venue_type, "其它"),
                m.ccf_rank or "",
                j.paper_count,
                j.total_citations,
                f"{m.cas_quartile}区" if m.cas_quartile else "",
                m.letpub_if if m.letpub_if is not None else "",
                round(m.impact_2yr, 2) if m.impact_2yr is not None else "",
                cycle,
                m.acceptance or "",
            ))

    def _sort_tree(self, col: str) -> None:
        """点表头就地排序，不用重跑检索。"""
        items = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]

        def as_num(text: str):
            try:
                return float(str(text).replace("区", ""))
            except ValueError:
                return None

        filled = [p for p in items if str(p[0]).strip()]
        blanks = [p for p in items if not str(p[0]).strip()]
        numeric = bool(filled) and all(as_num(v) is not None for v, _ in filled)

        if not hasattr(self, "_sort_desc"):
            self._sort_desc = {}
        desc = not self._sort_desc.get(col, False)
        self._sort_desc[col] = desc

        filled.sort(key=lambda p: as_num(p[0]) if numeric else str(p[0]).lower(),
                    reverse=desc)
        # 空值单独拼在末尾：无论升序降序都沉底，
        # 否则降序时「没有分区/没有 IF」的刊会占满前排
        for index, (_, k) in enumerate(filled + blanks):
            self.tree.move(k, "", index)

    def _open_row(self, _event) -> None:
        sel = self.tree.selection()
        if not sel or self.result is None:
            return
        try:
            journal = self.result.journals[int(sel[0])]
        except (ValueError, IndexError):
            return
        url = journal.metrics.homepage or journal.metrics.letpub_url
        if not url and journal.papers:
            url = journal.papers[0].url
        if url:
            webbrowser.open(url)
        else:
            messagebox.showinfo("没有链接", f"「{journal.name}」暂无可打开的链接。")

    def _open_dir(self) -> None:
        target = self.out_files[0].parent if self.out_files else Path(self.e_out.get())
        if not target.exists():
            messagebox.showinfo("目录不存在", str(target))
            return
        if sys.platform == "win32":
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=False)
        else:
            subprocess.run(["xdg-open", str(target)], check=False)

    def _open_html(self) -> None:
        for path in self.out_files:
            if path.suffix == ".html":
                webbrowser.open(path.resolve().as_uri())
                return

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel("还在跑", "检索还没结束，确定要退出吗？"):
                return
            self.stop_flag.set()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        # Windows 高分屏下不做这个设置，界面会发虚
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
