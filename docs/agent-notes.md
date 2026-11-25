# Agent Notes from the CLI: `sb notes` and `sb mockups`

`sb notes` publishes markdown or HTML files to an
[Agent Notes](https://github.com/alimuzzaman/agent-notes) service and manages them.
`sb mockups` publishes a folder as a versioned mockup. Any shell,
script, durable job or remote session that can run `./sb` can use them, with no
MCP client setup.

## How the token is held

The commands call the service's MCP endpoint (`<url>/mcp`) with a bearer token.
The token is a secret held by the Sandbox secrets broker:

- It is stored as one key in a broker source, by default the `personal` source
  (`~/.zshrc.secrets`, or `$SANDBOX_SECRETS_FILE`), under
  `SANDBOX_NOTES_TOKEN_<REMOTE>` (for `xspeed-dev`: `SANDBOX_NOTES_TOKEN_XSPEED_DEV`).
- Every command runs the HTTP calls in a broker child process
  (`sandbox/notes/child.py`, the `sandbox-notes-mcp` use profile). The token
  is in that child's environment only. The broker redacts it from the child's
  output and audits each use in `$SANDBOX_HOME/runtime/secrets/audit.jsonl`
  without the value.
- The token never goes in argv, a log, command output or config. Requests and
  responses pass through a `0700` work directory under
  `$SANDBOX_HOME/runtime/notes/work/`, which is deleted after each batch.

## First-time setup (the owner runs this once per machine)

```sh
./sb notes login --remote xspeed-dev --url https://notes.xspeedcdn.online
```

`login` works only from a machine on the tailnet, because it relies on the
service's tailnet trust: opening the dashboard there signs you in with no
password. It then:

1. records `xspeed-dev -> https://notes.xspeedcdn.online` in
   `$SANDBOX_HOME/sandbox.local.yml` (and makes it the default remote if none is set);
2. reads the dashboard's CSRF field, revokes every live token named
   `sb-notes-<host>` (`--name` picks another name), and creates a new one
   through the dashboard form;
3. stores the new token with the broker (`SecretService.set`; it creates an
   empty `0600` personal file if there is none), then makes one `list_notes`
   call to check it (`--no-verify` skips the check).

Running `login` again rotates the token: the old one with the same name is
revoked. `login` refuses inside a durable job.

The broker does not require a human approval for this store, but minting a
credential is the owner's call. Agents should not run `login` unless the owner
asked for it.

### Where the remote name comes from

`--remote NAME` selects a service from the `notes` block. With no `--remote`,
`notes.default_remote` is used. The name is only a label for the service; it
need not be a registered `sb remote`.

```yaml
# $SANDBOX_HOME/sandbox.local.yml (no secrets here)
notes:
  default_remote: xspeed-dev
  services:
    xspeed-dev:
      url: https://notes.xspeedcdn.online
      # token: {source: personal, key: SANDBOX_NOTES_TOKEN_XSPEED_DEV}   # the default
```

`sb notes config` shows the resolved service and token location.
`sb notes config --remote R --url U [--default]` records a service without minting
a token.

### On xspeed-dev itself (jobs and remote shells)

Commands that run on the server (a durable job, `sb host exec`, an SSH shell)
use the server's own broker and config. Give the server its own token once,
in either of two ways:

- If the server can reach the service over the tailnet listener, run
  `./sb notes login --remote xspeed-dev --url https://notes.xspeedcdn.online`
  on the server. That mints a separate token named `sb-notes-<server host>`.
- Otherwise copy the controller's token over SSH stdin and record the service:

  ```sh
  ./sb secrets copy --source personal --key SANDBOX_NOTES_TOKEN_XSPEED_DEV --remote xspeed-dev
  # then, on the server:
  ./sb notes config --remote xspeed-dev --url https://notes.xspeedcdn.online --default
  ```

## `sb notes`

Every action takes `--remote NAME` and `--json` (one JSON object on stdout;
`"ok": false` with an `error.code` and exit 1 on failure).

| Command | What it does |
|---|---|
| `create --file F [--title T] [--page] [--collection C] [--public]` | Publishes a Markdown note or, with `--page`, one complete HTML document. `--html` is an alias for `--page`. Prints the slug and URL. `--public` serves the item on the public host. |
| `update <slug> --file F [--title T]` | Replaces the content. Same slug, same URL. |
| `get <slug>` | Prints the content (`--json`: the note, its assets included). |
| `list [--kind note\|page] [--collection C] [--query Q]` | Lists your notes and pages, newest first. `--query` searches titles and content. |
| `delete <slug>` | Deletes the note and its assets. |
| `sync <dir> [--prune] [--adopt \| --create-duplicates]` | One note per `*.md` file in `dir` (not recursive; dotfiles skipped). See below. |
| `upload <slug> <file>` | Attaches a PNG, JPEG, GIF or WebP image. Prints the path to embed. |
| `share <slug> --ttl 24h` | Creates an expiring read-only link on the public host and prints it. `--ttl` takes hours (`24`, `24h`) or days (`7d`). The service caps it. |
| `share <slug> --revoke` | Revokes every active link on the note. |

A share link is a secret: the service shows it once. `share --ttl` refuses inside
a durable job, because job output is retained.

### `sync`

`sync` keeps a folder of markdown files published:

- The folder name becomes the collection. Notes in a different collection are not adoption candidates.
- The map `<dir>/.notes.json` records `file -> {slug, url, title, sha256, collection}` and
  the remote it belongs to. A map for another remote is refused.
- A file with no map entry is first looked up among your existing notes, so a
  lost map, or notes another client (such as an MCP session) already published,
  do not turn into duplicates. Candidates are tailnet markdown notes that this
  map does not already hold; public and HTML notes never are. A file matches
  the note whose stored filename is the file's name as the service slugifies it
  (`11-php-versions-7.4-8.4.md` is stored as `11-php-versions-7-4-8-4.md`).
  Files with no filename match then match by title, but only notes published
  without a filename (stored under their title's slug) qualify, so a note sync
  made from another folder is never matched by its heading.
  - If the match already has the file's exact content, sync adopts it: same URL,
    reported as `adopted`, content unchanged; its title and download filename
    are set from the file.
  - If the content differs, the file fails with `notes_adopt_differs` and
    nothing changes. To adopt that one note anyway, put its slug in
    `.notes.json`; `--adopt` overwrites the match for every unmapped file in
    the run.
  - Two matches, or one note matching two files, fail with
    `notes_adopt_ambiguous`.
  - `--create-duplicates` skips the lookup and publishes new notes.
  - A file that matches nothing is created.
  - An adopted note is shared with any other folder whose map holds it: a later
    edit in either folder overwrites it, and `--prune` in one deletes it for
    both.
- Every note sync creates or updates stores the file name (without `.md`) as
  its download filename, so it can be found again the same way. A file whose content or first heading
  changed is updated (same URL). An unchanged file makes no call.
- If a mapped note was deleted on the service, the file is published again as
  a new note (`recreated`, new URL).
- A map entry whose file is gone is reported as `orphaned`. Only `--prune`
  deletes its note.
- Calls go in batches of 25, and the map is saved after each batch. Ctrl-C or
  a client that dies part way keeps every note it reported back.
- If `sb` is killed (SIGKILL, SIGTERM, a closed terminal), or a call is cut off
  mid-request, a created note can be left without a map entry. The next run
  then creates a duplicate and the first copy stays on the service; delete it
  with `sb notes list` and `sb notes delete`.
- Exit 1 if any file failed. Failures are listed with their error code.

## `sb mockups`

A mockup keeps a folder's published files together. Each update publishes a
complete new version. Earlier versions still have their own URLs and files.
Links and assets should use relative paths. If the folder has no `index.html`,
Notes lists its screens and files.

| Command | What it does |
|---|---|
| `publish <dir> --name N [--title T] [--message M]` | Reserves a new name and publishes version 1. Use `update` if the name is already yours. |
| `update N <dir> [--message M]` | Publishes the folder as the next version. The main URL stays the same. |
| `list [--query Q] [--prefix P]` | Lists your mockups. |
| `get N [--version V]` / `versions N` | Shows a version or lists the full history. |
| `pull N <dir> [--version V] [--force]` | Downloads a version and verifies each file before writing. `--force` replaces matching paths in an existing directory. |
| `share N --ttl 24h [--version V]` | Creates a public link pinned to one version. It defaults to the current version. |
| `share N --revoke` / `delete N` | Revokes active links or deletes the mockup and all its versions. |

Use names with 1–120 lowercase letters, digits or hyphens, starting with a
letter or digit. Put only the files you want viewers to see in the folder.
Run `publish` or `update` with `--dry-run` to list those files before
uploading. The CLI skips dotfiles and hidden directories and refuses Git
checkouts, symlinks and special files. A version can hold up to 1,000 files,
5 MiB per file and 25 MiB total.

Notes stages files before publishing. If the commit reply is lost, check
`sb mockups versions N` before uploading again. Mockups are private to the
Tailnet unless you create a share link. Older one-document mockups remain
available at their original note URLs and through `sb notes`.
For versioned mockups, set the title at publish time. The old
`update --title` and `publish --public` options have been removed.

Versioned mockups, collections and filtered note listing require the Notes
library feature on the server. The service must apply migration 003 and enable
the library, visibility and HTML features before the client uses these commands.

The broker child runs with a minimal environment (`PATH`, `HOME`, `TMPDIR`,
locale). It uses Python's default CA store; `SSL_CERT_FILE` and `SSL_CERT_DIR`
are not passed through.

## Errors

| Code | Meaning |
|---|---|
| `notes_remote_missing` / `notes_remote_unknown` | No `--remote` and no default, or no service for that name. Run `login --url` or `config --url`. |
| `notes_token_missing` | The broker has no token at the configured key. Run `login`. |
| `notes_unauthorized` | The service refused the token (revoked or wrong). Run `login`. |
| `notes_unreachable` | No connection, e.g. not on the tailnet. |
| `notes_bad_response` | The service answered with something other than a tool result, including any redirect (never followed, so the token cannot leave). |
| `notes_incomplete` | The client stopped (killed or timed out) before reporting this call. A call cut off mid-request may still have been applied. |
| `notes_tool_error` | The service refused the call; the message is the service's own. |
| `notes_login_untrusted` | The dashboard did not sign this machine in by tailnet trust. |
| `notes_adopt_ambiguous` | `sync`: an unmapped file has more than one match, or shares one with another file; it was skipped. |
| `notes_adopt_differs` | `sync`: an unmapped file matches an existing note with other content; nothing changed. Use `--adopt` or `--create-duplicates`. |
| `notes_refused_in_job` | `login` or `share --ttl` inside a durable job. |
| `mockup_exists` / `mockup_not_found` | `publish` over an existing name; any other action on a missing one. |
