---
name: note
description: note.com の公開記事を取得し、vault/10_raws/notes に type:note の生ソースとして書き出す。本文中の画像は vault/10_raws/assets に保存する。note に書いた記事を Wiki 取り込み(/ingest)の対象にしたいときに使う。
---

# Note

note.com に公開した記事を取得し、`vault/10_raws/notes/` へ `type: note` の生ソースとして書き出す。ディレクトリ構成・ファイル名・frontmatter の共通規約は `CLAUDE.md` の「`vault/10_raws/`」の節を参照すること。本スキルはワークフローと、`type: note` に固有の項目を定める。

このスキルは常に **1 回の呼び出しにつき 1 記事のみ** を対象にする (1 記事 = 1 raw ノート)。

HTML から Markdown への変換は同梱スクリプト `scripts/note_fetch.py` が決定論的に行う。LLM が本文を書き起こすことはしない (再取り込みしても差分が安定するため)。スクリプトは Python 標準ライブラリのみで動作し、認証も不要。

## 呼び出しモード

### 引数なし (`/note`)

以下を実行し、**まだ取り込んでいない記事** を一覧表示する。**それ以上は何もしない。** 取得・変換は一切行わず、ユーザが対象を選んで `/note <key|URL>` を実行するのを待つ。

```bash
python3 .claude/skills/note/scripts/note_fetch.py list --check-updates
```

- 出力は TSV (`key` / `date` / `status` / `title` / `url`)。`status` 列の意味は以下
  - `-` — 未取り込み。`/note <key>` の対象
  - `fetched:<ファイル名>` — 取り込み済みで、note 側の本文と一致している
  - `updated:<ファイル名>` — 取り込み済みだが note 側で記事が更新されている。再取り込みの対象 (下記「再取り込み」参照)
- `--check-updates` は取り込み済み記事の本文を 1 件ずつ取得して突き合わせる (note の API は記事の更新日時を公開していないため)。件数が多く時間がかかるときは付けずに実行してよい
- 記事が 0 件でもエラーにせず、その旨を伝えるだけに留める

### 引数あり (`/note <key|URL>`)

指定された 1 記事のみを対象に、下記の手順を実行する。記事 key (`n21c7ed322793` など) でも記事 URL (`https://note.com/crz33/n/n21c7ed322793`) でも受け付ける。

- 複数記事が指定された場合や、まとめて処理するようユーザから指示された場合でも、1 回の呼び出しでは 1 記事だけを処理する。残りは「次はどの記事を処理しますか」と確認し、`/note <key>` を個別に呼び直してもらう

## 手順 (引数ありの場合のみ実行)

1. プレビューして内容とタイトルを確認する (ファイルは書かれない)

   ```bash
   python3 .claude/skills/note/scripts/note_fetch.py fetch <key> --stdout
   ```

2. 記事タイトルから短い英数スラッグを決める。既存の `vault/10_raws/clips/` (`20260802_llm-wiki` `20260809_OKF` など) と同じ流儀で、小文字ハイフン区切りの 2〜4 語程度にする。`find vault/10_raws/ -iname "*<slug>*"` で同名がないことを確認する
3. 書き出す

   ```bash
   python3 .claude/skills/note/scripts/note_fetch.py fetch <key> --slug <slug>
   ```

   - `vault/10_raws/notes/YYYYMMDD_<slug>.md` が作られ、本文中の画像は `vault/10_raws/assets/YYYYMMDD_<slug>_NN.<ext>` に保存されて `![[10_raws/assets/…]]` で参照される
   - 既存ファイルがある場合スクリプトは書き込まずエラーにする。上書きしてよいかは**ユーザに確認**し、許可されたときだけ `--force` を付けて再実行する (下記「再取り込み」参照)
   - 有料・一部有料の記事は本文が途中で切れるためスクリプトが停止する。原則として取り込まない (ユーザが明示的に望んだ場合のみ `--allow-limited`)
4. 書き出した内容がスクリプトの出力と一致するか確認する

   ```bash
   diff <(python3 .claude/skills/note/scripts/note_fetch.py fetch <key> --slug <slug> --stdout 2>/dev/null) vault/10_raws/notes/YYYYMMDD_<slug>.md
   ```

   差分が出た場合、Obsidian 側 (Templater の「ファイル作成時にテンプレートを適用」など) が新規ファイルを書き換えている。記事本文が正なので、スクリプトの出力に合わせて復元し、その旨をユーザに報告する
5. 書き出した Markdown を読み、以下を整える。それ以外の本文には手を入れない (生ソースは note 側の記事が正であるため)
   - `description` — 空で出力されるので、記事内容の 1 行要約を書き入れる
   - `tags` — `note` と note 側のハッシュタグが入っている。内容から補うべきタグがあれば追加する
   - 変換崩れ (見出しレベル・コードブロック・リスト・画像) があれば直す
6. `status` は `draft` のまま残す。`stable` への更新はユーザの判断であり、このスキルは行わない
7. 作業完了後、生成したファイルパス・保存した画像の件数・スクリプトが出した警告 (未知タグなど)・手順 4〜5 で行った修正をユーザに簡潔に報告する

## 再取り込み (note 側で記事を更新した場合)

`list --check-updates` で `updated:` が立った記事は、note 側の本文がローカルの生ソースと食い違っている。この場合は同じスラッグで上書きし直す。

```bash
python3 .claude/skills/note/scripts/note_fetch.py fetch <key> --slug <既存と同じ slug> --force
```

- ファイルは**丸ごと作り直される**。`description` は空に戻り、ユーザが手で足したタグや独自項目も消えるため、手順 4〜5 をもう一度行う。上書き前の内容で残したいものがあれば、実行前に確認しておく
- スクリプトは既存の `status` が `draft` でなかった場合それを `draft` に戻し、warning を出す。CLAUDE.md の遷移ルール (`ingest 済みのソースを直す場合は draft に戻す`) に沿った動作である
- 上書き後は Wiki 側が古い内容のままなので、**ユーザに再 ingest を促す**。ユーザが内容を確認して `status: stable` にしたら `/ingest <path>` を実行する。既に `20_wiki/sources/` に対応ページがある場合、`/ingest` はそれを新規作成せず更新する

## 注意

- `vault/10_raws/` は本来 LLM が新規作成しない不変レイヤーだが、`notes/` `assets/` への note.com 記事の書き出しに限り本スキルが例外的に新規作成することが `CLAUDE.md` で認められている。`clips/` `meetings/` のファイルを本スキルが作成・書き換えすることはない
- 記事の本文を LLM が要約・改変しない。note.com の記事がそのまま一次ソースであり、要約は `/ingest` が `20_wiki/sources/` 側で行う
- note のキャプション (`figcaption`) は本文の段落にしない。画像は埋め込みの alt (`![[…|キャプション]]`)、引用は引用ブロック内の最終行 (`> — 出典`) に畳む。Markdown で表現できない体裁は取り込まない方針である
- スクリプトが「未知のタグ」を警告した場合、その部分の変換が落ちている可能性がある。ユーザに報告し、必要なら `scripts/note_fetch.py` の `KNOWN_TAGS` と変換規則の追加を提案する
- Obsidian が起動していると、vault に新規作成したファイルに Templater の「ファイル作成時にテンプレートを適用」が反応し、本文中の `<% ... %>` を評価して書き換えてしまうことがある。手順 4 の diff で必ず確認する
- note のユーザ名は `scripts/note_fetch.py` の `DEFAULT_USER` (`crz33`) を既定値とする。別ユーザを見る場合は `--user` を渡す
