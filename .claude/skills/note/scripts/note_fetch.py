#!/usr/bin/env python3
"""note.com の公開記事を vault/10_raws/notes/ の生ソースとして取り込む。

標準ライブラリのみで動作する。使い方は .claude/skills/note/SKILL.md を参照。

  list                     記事一覧を TSV で出力 (取り込み済みは fetched:<ファイル名>)
  list --check-updates     さらに本文を突き合わせ、note 側で更新された記事に updated を立てる
  fetch <key|URL>          記事 1 件を Markdown 化して書き出す (--stdout でプレビュー)
"""

import argparse
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser

DEFAULT_USER = "crz33"
UA = "Mozilla/5.0 (compatible; llm-wiki note fetcher)"

# 変換で扱うタグ。ここに無いタグが本文に出てきたら警告する
KNOWN_TAGS = {
    "p", "br", "h2", "h3", "h4", "h5", "h6",
    "strong", "b", "em", "i", "a", "ul", "ol", "li",
    "blockquote", "pre", "code", "figure", "figcaption", "img",
    "table-of-contents", "div", "span",
}

# 終了タグを持たない要素。入れ子の深さを数える際に無視する
VOID_TAGS = {"br", "img", "hr", "wbr", "source", "embed"}


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res)


def http_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read()


# --------------------------------------------------------------------------
# HTML -> Markdown
# --------------------------------------------------------------------------


class NoteHTMLParser(HTMLParser):
    """note の本文 HTML を Markdown に変換する。

    note の本文は p/h2/h3/ul/li/pre>code/blockquote/figure と少数のインライン
    タグしか使わないため、ブロック単位の素朴な状態機械で足りる。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []          # 確定した Markdown ブロック
        self.buf = []             # 現在組み立て中のインラインテキスト
        self.unknown_tags = set()
        self.images = []          # (url, キャプション) を出現順に記録
        self.list_stack = []      # ["ul"|"ol", 連番] のスタック
        self.li_stack = []        # li ごとの確定ブロック (note は li>p の構造を使う)
        self.in_pre = False
        self.pre_buf = []
        self.in_figure = False
        self.figure_blocks = []   # figure 内で確定したブロック
        self.in_figcaption = False
        self.in_blockquote = False
        self.quote_blocks = []
        self.skip_depth = 0       # table-of-contents などを丸ごと捨てる

    # -- ヘルパ ------------------------------------------------------------

    def _flush(self):
        """組み立て中のインラインテキストを 1 ブロックとして確定する。"""
        text = "".join(self.buf).strip()
        self.buf = []
        if text:
            self._emit(text)

    def _emit(self, block):
        if self.in_figcaption:
            self.figure_blocks.append(block)
        elif self.in_blockquote:
            self.quote_blocks.append(block)
        elif self.li_stack:
            self.li_stack[-1].append(block)
        elif self.in_figure:
            self.figure_blocks.append(block)
        else:
            self.blocks.append(block)

    # -- HTMLParser の口 ---------------------------------------------------

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in KNOWN_TAGS:
            self.unknown_tags.add(tag)
        if self.skip_depth:
            if tag not in VOID_TAGS:
                self.skip_depth += 1
            return

        if tag == "table-of-contents":
            self._flush()
            self.skip_depth = 1
        elif tag == "pre":
            self._flush()
            self.in_pre = True
            self.pre_buf = []
        elif tag == "code" and not self.in_pre:
            self.buf.append("`")
        elif tag == "br":
            self.buf.append("\n" if self.in_pre else "  \n")
        elif tag in ("strong", "b"):
            self.buf.append("**")
        elif tag in ("em", "i"):
            self.buf.append("*")
        elif tag == "a":
            self._href = attrs.get("href", "")
            self._href_at = len(self.buf)
            self.buf.append("[")
        elif tag in ("ul", "ol"):
            self._flush()
            self.list_stack.append([tag, 0, []])
        elif tag == "li":
            self._flush()
            self.li_stack.append([])
        elif tag == "blockquote":
            self._flush()
            self.in_blockquote = True
            self.quote_blocks = []
        elif tag == "figure":
            self._flush()
            self.in_figure = True
            self.figure_blocks = []
        elif tag == "figcaption":
            self._flush()
            self.in_figcaption = True
        elif tag == "img":
            src = attrs.get("src", "")
            if src:
                alt = attrs.get("alt", "")
                self.images.append((src, alt))
                # 画像はプレースホルダで置き、後段で実ファイル名に差し替える
                self._emit("\x00IMG%d\x00" % (len(self.images) - 1))
        elif tag in ("p", "h2", "h3", "h4", "h5", "h6"):
            self._flush()

    def handle_endtag(self, tag):
        if self.skip_depth:
            self.skip_depth -= 1
            return

        if tag == "pre":
            code = "".join(self.pre_buf).strip("\n")
            self.in_pre = False
            self.pre_buf = []
            self._emit("```\n" + code + "\n```")
        elif tag == "code" and not self.in_pre:
            self.buf.append("`")
        elif tag in ("strong", "b"):
            self.buf.append("**")
        elif tag in ("em", "i"):
            self.buf.append("*")
        elif tag == "a":
            href = self._href or ""
            # note の埋め込みコンテンツはリンク文字列が空になるので URL 自体を出す
            if self._href_at is not None and not "".join(self.buf[self._href_at + 1:]).strip():
                del self.buf[self._href_at:]
                self.buf.append(href)
            else:
                self.buf.append("](%s)" % href)
            self._href = None
            self._href_at = None
        elif tag == "li":
            self._flush()
            blocks = self.li_stack.pop() if self.li_stack else []
            text = "\n".join(b for b in blocks if b.strip())
            if text and self.list_stack:
                entry = self.list_stack[-1]
                indent = "  " * (len(self.list_stack) - 1)
                if entry[0] == "ol":
                    entry[1] += 1
                    marker = "%d. " % entry[1]
                else:
                    marker = "- "
                lines = text.split("\n")
                item = [indent + marker + lines[0]]
                # li 内の 2 行目以降 (入れ子リストや複数段落) はぶら下げインデント
                item += [
                    ln if ln.startswith(" ") else indent + "  " + ln for ln in lines[1:]
                ]
                entry[2].append("\n".join(item))
        elif tag in ("ul", "ol"):
            self._flush()
            if self.list_stack:
                items = self.list_stack.pop()[2]
                if items:
                    self._emit("\n".join(items))
        elif tag == "figcaption":
            text = "".join(self.buf).strip()
            self.buf = []
            self.in_figcaption = False
            if text:
                self._caption = text
        elif tag == "blockquote":
            self._flush()
            self.in_blockquote = False
            body = "\n\n".join(self.quote_blocks)
            self.quote_blocks = []
            quoted = "\n".join(
                ("> " + line if line else ">") for line in body.split("\n")
            )
            if body:
                self._emit(quoted)
        elif tag == "figure":
            self._flush()
            self.in_figure = False
            blocks = self.figure_blocks
            self.figure_blocks = []
            caption = self._caption
            self._caption = None
            # figcaption は本文の段落にしない。画像なら alt、引用なら引用内の出典行に畳む
            if caption and blocks:
                m = re.match(r"^\x00IMG(\d+)\x00$", blocks[-1])
                if m:
                    src, _ = self.images[int(m.group(1))]
                    self.images[int(m.group(1))] = (src, caption)
                    caption = None
                elif blocks[-1].startswith(">"):
                    blocks[-1] += "\n>\n> — %s" % caption
                    caption = None
            for b in blocks:
                self.blocks.append(b)
            if caption:
                # 画像でも引用でもない figure。落とすと情報が消えるので段落として残す
                self.blocks.append(caption)
        elif tag in ("p", "h2", "h3", "h4", "h5", "h6"):
            text = "".join(self.buf).strip()
            self.buf = []
            if not text:
                return
            if tag.startswith("h"):
                # note の本文最上位は h2。CLAUDE.md の H1 重複禁止と整合する
                self._emit("#" * int(tag[1]) + " " + text)
            else:
                self._emit(text)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in ("br", "img"):
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.in_pre:
            self.pre_buf.append(data)
        else:
            self.buf.append(data.replace("\n", " "))

    # <a> の href は starttag で拾い endtag で使う。figcaption も同様に持ち越す
    _href = None
    _href_at = None
    _caption = None


def html_to_markdown(body_html):
    """note の body HTML を (markdown, images, unknown_tags) に変換する。"""
    p = NoteHTMLParser()
    p.feed(body_html)
    p.close()
    p._flush()
    md = "\n\n".join(b for b in p.blocks if b.strip())
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md, p.images, sorted(p.unknown_tags - KNOWN_TAGS)


# --------------------------------------------------------------------------
# note API
# --------------------------------------------------------------------------


def list_contents(user):
    """公開記事を新しい順に返す。"""
    out = []
    page = 1
    while True:
        url = (
            "https://note.com/api/v2/creators/%s/contents?kind=note&page=%d"
            % (urllib.parse.quote(user), page)
        )
        data = http_json(url)["data"]
        for c in data.get("contents", []):
            out.append(
                {
                    "key": c["key"],
                    "date": (c.get("publishAt") or "")[:10],
                    "title": c.get("name", ""),
                    "url": "https://note.com/%s/n/%s" % (user, c["key"]),
                }
            )
        if data.get("isLastPage", True):
            break
        page += 1
    return out


def get_note(key):
    return http_json("https://note.com/api/v3/notes/%s" % urllib.parse.quote(key))["data"]


def normalize_key(arg):
    """記事 key そのものでも記事 URL でも受け付ける。"""
    m = re.search(r"(n[0-9a-z]{10,})", arg)
    if not m:
        sys.exit("error: 記事 key を特定できない: %s" % arg)
    return m.group(1)


def ingested_keys(notes_dir):
    """既に取り込み済みの note_key を集める。key -> ファイル名。"""
    keys = {}
    if not os.path.isdir(notes_dir):
        return keys
    for name in sorted(os.listdir(notes_dir)):
        if not name.endswith(".md"):
            continue
        with open(os.path.join(notes_dir, name), encoding="utf-8") as f:
            head = f.read(2000)
        m = re.search(r"^note_key:\s*(\S+)\s*$", head, re.M)
        if m:
            keys[m.group(1)] = name
    return keys


def split_frontmatter(text):
    """(frontmatter, 本文) に分ける。frontmatter が無ければ ("", 全文)。"""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        if end != -1:
            return text[4:end + 1], text[end + 5:]
    return "", text


def fm_value(frontmatter, key):
    m = re.search(r"^%s:\s*(.*?)\s*$" % re.escape(key), frontmatter, re.M)
    return m.group(1) if m else None


def body_differs(key, path, user):
    """note 側の本文とローカルファイルの本文が食い違うか。

    note の API は記事の更新日時を公開していないため、本文を突き合わせて判定する。
    frontmatter (description や tags のユーザ編集) は比較対象にしない。
    """
    with open(path, encoding="utf-8") as f:
        local_body = split_frontmatter(f.read())[1]
    note = get_note(key)
    md, images, _ = html_to_markdown(note.get("body") or "")
    base_name = os.path.splitext(os.path.basename(path))[0]
    for i, (src, caption) in enumerate(images, start=1):
        md = md.replace(
            "\x00IMG%d\x00" % (i - 1),
            asset_link(asset_filename(src, base_name, i), caption),
        )
    return md.strip() != local_body.strip()


# --------------------------------------------------------------------------
# 出力
# --------------------------------------------------------------------------


def yaml_str(s):
    """frontmatter のスカラー値を安全に書く。"""
    if s == "" or re.search(r'^[\s>|&*!%@`\[{#-]|[:#]\s|["\']|[:\[\]{},]$', s):
        return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')
    return s


def build_frontmatter(note, key, user, hashtags):
    lines = ["---"]
    lines.append("title: %s" % yaml_str(note.get("name", "")))
    lines.append("source: https://note.com/%s/n/%s" % (user, key))
    lines.append("author:")
    lines.append("  - %s" % user)
    lines.append("created: %s" % (note.get("publish_at") or "")[:10])
    lines.append('description: ""')
    lines.append("tags:")
    lines.append("  - note")
    for t in hashtags:
        lines.append("  - %s" % yaml_str(t))
    lines.append("type: note")
    lines.append("status: draft")
    lines.append("note_key: %s" % key)
    lines.append("---")
    return "\n".join(lines)


def asset_filename(src, base_name, i):
    ext = os.path.splitext(urllib.parse.urlparse(src).path)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
        ext = ".png"
    return "%s_%02d%s" % (base_name, i, ext)


def asset_link(fname, caption):
    """Obsidian の画像埋め込み。note のキャプションは alt (エイリアス) に畳む。"""
    if caption:
        # `|` は埋め込みの区切り、`]]` はリンクの終端なので潰す
        caption = caption.replace("|", "/").replace("]]", "]")
        return "![[10_raws/assets/%s|%s]]" % (fname, caption)
    return "![[10_raws/assets/%s]]" % fname


def download_images(images, assets_dir, base_name, dry_run):
    """画像を assets に落とし、プレースホルダの置換先リンクを返す。"""
    links = []
    saved = []
    for i, (src, caption) in enumerate(images, start=1):
        fname = asset_filename(src, base_name, i)
        links.append(asset_link(fname, caption))
        if dry_run:
            continue
        data = http_bytes(src)
        os.makedirs(assets_dir, exist_ok=True)
        with open(os.path.join(assets_dir, fname), "wb") as f:
            f.write(data)
        saved.append(fname)
    return links, saved


def cmd_list(args):
    notes_dir = os.path.join(args.vault, "10_raws", "notes")
    done = ingested_keys(notes_dir)
    contents = list_contents(args.user)
    updated = 0
    print("key\tdate\tstatus\ttitle\turl")
    for c in contents:
        name = done.get(c["key"])
        if name is None:
            mark = "-"
        elif args.check_updates and body_differs(c["key"], os.path.join(notes_dir, name), args.user):
            mark = "updated:%s" % name
            updated += 1
        else:
            mark = "fetched:%s" % name
        print("\t".join([c["key"], c["date"], mark, c["title"], c["url"]]))
    summary = "\n%d 件 (取り込み済み %d 件" % (len(contents), len(done))
    summary += ", note 側で更新 %d 件)" % updated if args.check_updates else ")"
    print(summary, file=sys.stderr)
    if not args.check_updates and done:
        print(
            "note 側の更新を確認するには --check-updates を付ける (記事数だけ API を叩く)",
            file=sys.stderr,
        )


def cmd_fetch(args):
    key = normalize_key(args.key)
    note = get_note(key)

    if note.get("is_limited") or (note.get("price") or 0) > 0:
        if not args.allow_limited:
            sys.exit(
                "error: 有料・一部有料の記事は本文が途中で切れる (price=%s, is_limited=%s)。\n"
                "       それでも取り込む場合は --allow-limited を付ける。"
                % (note.get("price"), note.get("is_limited"))
            )
        print("warning: 有料記事のため本文が欠けている可能性がある", file=sys.stderr)

    date = (note.get("publish_at") or "")[:10]
    hashtags = [
        (h.get("hashtag") or {}).get("name", "").lstrip("#")
        for h in (note.get("hashtag_notes") or [])
    ]
    hashtags = [h for h in hashtags if h]

    md, images, unknown = html_to_markdown(note.get("body") or "")
    if unknown:
        print("warning: 未知のタグ: %s" % ", ".join(unknown), file=sys.stderr)

    if args.stdout:
        base_name = "%s_%s" % (date.replace("-", ""), args.slug or "SLUG")
    else:
        if not args.slug:
            sys.exit("error: --slug が必要 (--stdout でプレビューしてから決める)")
        base_name = "%s_%s" % (date.replace("-", ""), args.slug)

    out_path = os.path.join(args.vault, "10_raws", "notes", base_name + ".md")
    old_status = None
    if not args.stdout and os.path.exists(out_path):
        if not args.force:
            sys.exit("error: 既にファイルがある: %s (上書きするなら --force)" % out_path)
        with open(out_path, encoding="utf-8") as f:
            old_status = fm_value(split_frontmatter(f.read())[0], "status")

    assets_dir = os.path.join(args.vault, "10_raws", "assets")
    links, saved = download_images(images, assets_dir, base_name, args.stdout)
    for i, link in enumerate(links):
        md = md.replace("\x00IMG%d\x00" % i, link)

    doc = build_frontmatter(note, key, args.user, hashtags) + "\n\n" + md + "\n"

    if args.stdout:
        sys.stdout.write(doc)
        print(
            "\n--- preview (未書き込み): 画像 %d 件, タイトル: %s"
            % (len(images), note.get("name", "")),
            file=sys.stderr,
        )
        return

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    print("wrote: %s" % out_path)
    for s in saved:
        print("asset: %s" % os.path.join("vault", "10_raws", "assets", s))
    if old_status is not None:
        print("overwrote: 既存ファイルを上書きした (description は空に戻る)")
        if old_status != "draft":
            print(
                "warning: status を %s から draft に戻した。"
                "Wiki へ反映し直すには内容を確認して status: stable にし、/ingest を実行する"
                % old_status
            )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--user", default=DEFAULT_USER, help="note のユーザ名 (既定: %s)" % DEFAULT_USER)
    ap.add_argument("--vault", default="vault", help="vault ディレクトリ (既定: vault)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="記事一覧を TSV で出力")
    p_list.add_argument(
        "--check-updates",
        action="store_true",
        help="取り込み済み記事の本文を note 側と突き合わせ、更新されたものに updated を立てる",
    )
    p_list.set_defaults(func=cmd_list)

    p_fetch = sub.add_parser("fetch", help="記事 1 件を Markdown 化")
    p_fetch.add_argument("key", help="記事 key または記事 URL")
    p_fetch.add_argument("--slug", help="ファイル名に使う英数スラッグ")
    p_fetch.add_argument("--stdout", action="store_true", help="書き込まず標準出力へ")
    p_fetch.add_argument("--force", action="store_true", help="既存ファイルを上書き")
    p_fetch.add_argument("--allow-limited", action="store_true", help="有料記事でも続行")
    p_fetch.set_defaults(func=cmd_fetch)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
