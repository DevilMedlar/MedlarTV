import ast
import json
import time
from pathlib import Path
import argparse
import yaml

# Catalog builds inspect dispatch metadata without importing handlers or starting services.
def implemented_commands(root: Path) -> dict[str, dict]:
    tree = ast.parse((root / "core" / "command_handlers.py").read_text(encoding="utf-8"))
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value
    registry = next(node.value for node in tree.body
                    if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "COMMAND_REGISTRY")
    def value(node):
        if isinstance(node, ast.Name):
            if node.id == "TRANSLATE_HELP":
                translation = ast.parse((root / "core" / "translation_command.py").read_text(encoding="utf-8"))
                return next(ast.literal_eval(n.value) for n in translation.body
                            if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "HELP" for t in n.targets))
            return constants[node.id]
        if isinstance(node, ast.List):
            return [value(item) for item in node.elts]
        return ast.literal_eval(node)
    result = {}
    for name, info in zip(registry.keys, registry.values):
        result[value(name)] = {value(key): value(item) for key, item in zip(info.keys, info.values)
                               if value(key) != "handler"}
    # Listener-inline commands keep their existing role and primary-room boundaries.
    result.update({
        "forget": {"permissions": ["everybody"], "description": "Remove your saved facts", "usage": "!forget <fact|all>", "scope": "Your memory in the primary channel"},
        "forgetme": {"permissions": ["everybody"], "description": "Clear your saved facts and this chat's memory", "usage": "!forgetme", "scope": "Your memory in the primary channel"},
        "addcopilot": {"permissions": ["pilot", "mod"], "primary_effect": "always"},
        "removecopilot": {"permissions": ["pilot", "mod", "copilot"], "primary_effect": "always"},
        "listcopilots": {"permissions": ["everybody"]},
    })
    return result


def load_commands() -> list[dict]:
    root = Path(__file__).resolve().parents[1]
    data = yaml.safe_load((root / "config" / "commands.yaml").read_text(encoding="utf-8")) or {}
    configured = data.get("commands", {})
    if not isinstance(configured, dict):
        raise ValueError("Command configuration must be a mapping")
    for name, info in configured.items():
        if not isinstance(name, str) or not name or not isinstance(info, dict):
            raise ValueError("Invalid command metadata row")
        roles = info.get("allowed_roles", [])
        if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
            raise ValueError("Command roles must be a string list")
        if not all(isinstance(info.get(key, ""), str) for key in ("description", "usage", "response")):
            raise ValueError("Command text fields must be strings")
    implemented = implemented_commands(root)
    items = []
    for name in dict.fromkeys([*configured, *implemented]):
        info = configured.get(name, {})
        dispatch = implemented.get(name)
        item = {"name": str(name), "response": str(info.get("response", "")),
                "allowed_roles": list(dispatch["permissions"] if dispatch else info.get("allowed_roles", [])),
                "description": str(info.get("description") or (dispatch or {}).get("description", "")),
                "usage": str(info.get("usage") or (dispatch or {}).get("usage", "")),
                "implemented": dispatch is not None, "requirements": ""}
        if dispatch:
            if name in {"title", "game"}:
                item["set_roles"] = ["pilot", "copilot"]
                item["requirements"] = "Read: everybody. Set: pilot/copilot with verified primary-channel identity."
            elif dispatch.get("primary_effect") == "always":
                item["requirements"] = "Requires verified primary-channel identity."
            elif dispatch.get("scope"):
                item["requirements"] = dispatch["scope"]
        else:
            item["requirements"] = "Unavailable: no command dispatcher is implemented."
        items.append(item)
    return items


def build_once(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    items = load_commands()
    (out / "commands.json").write_text(json.dumps({"commands": items}, ensure_ascii=False), encoding="utf-8")
    (out / "index.html").write_text(render_commands_html(), encoding="utf-8")


def render_commands_html(*, api: bool = False) -> str:
    """Share literal rendering with the dormant API; hosting remains unchanged."""
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>MedlarTV Commands</title>
      <style>
        :root { --bg:#0f1218; --card:#1a1f2b; --text:#e6e9ef; --muted:#9aa4b2; --accent:#6ae3ff; }
        body { margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial; background:var(--bg); color:var(--text); }
        header { padding:20px 24px; border-bottom:1px solid #222633; display:flex; align-items:center; gap:12px; }
        header h1 { margin:0; font-size:18px; letter-spacing:.4px; }
        .container { max-width:960px; margin:24px auto; padding:0 16px; }
        .controls { display:flex; gap:12px; margin-bottom:16px; }
        input, select { background:var(--card); border:1px solid #2b3242; color:var(--text); padding:10px 12px; border-radius:8px; outline:none; }
        .grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(280px,1fr)); gap:14px; }
        .card { background:var(--card); border:1px solid #2b3242; border-radius:10px; padding:14px; }
        .name { font-weight:600; font-size:15px; }
        .roles { margin-top:6px; color:var(--muted); font-size:12px; }
        .desc { margin-top:8px; font-size:13px; color:var(--text); }
        .usage { margin-top:6px; font-size:12px; color:var(--muted); }
        .response { margin-top:10px; font-size:12px; color:#c8d2e0; border-top:1px dashed #2b3242; padding-top:8px; }
        .badge { display:inline-block; padding:3px 8px; border-radius:999px; background:#253047; color:#bfe8ff; margin-right:6px; font-size:11px; }
        .empty { color:var(--muted); text-align:center; padding:24px; }
        a { color:var(--accent); text-decoration:none; }
      </style>
    </head>
    <body>
      <header>
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12 2L2 7l10 5 10-5-10-5zm0 7l-10 5 10 5 10-5-10-5z" fill="#6ae3ff"/></svg>
        <h1>MedlarTV Command Catalog</h1>
      </header>
      <div class="container">
        <div class="controls">
          <input id="q" type="text" placeholder="Search commands…" />
          <select id="role">
            <option value="">All roles</option>
            <option>everybody</option>
            <option>pilot</option>
            <option>copilot</option>
            <option>mod</option>
            <option>vip</option>
          </select>
        </div>
        <p id="status" role="status">Loading catalog.</p>
        <div id="grid" class="grid"></div>
        <div id="empty" class="empty" style="display:none">No commands match your filters.</div>
        <p style="margin-top:18px; font-size:12px; color:#8fa1b5">Source: <a href="commands.json">commands.json</a></p>
      </div>
      <script>
        const state = { commands: [], q: '', role: '' };
        const grid = document.getElementById('grid');
        const empty = document.getElementById('empty');
        const q = document.getElementById('q');
        const role = document.getElementById('role');

        q.addEventListener('input', () => { state.q = q.value.toLowerCase(); render(); });
        role.addEventListener('change', () => { state.role = role.value; render(); });

        function matches(cmd) {
          const name = cmd.name.toLowerCase();
          const desc = (cmd.description||'').toLowerCase();
          const usage = (cmd.usage||'').toLowerCase();
          const text = name + ' ' + desc + ' ' + usage;
          const roles = cmd.allowed_roles;
          const roleOk = !state.role || state.role === 'pilot' || roles.includes('everybody') || roles.includes(state.role);
          const queryOk = !state.q || text.includes(state.q);
          return roleOk && queryOk;
        }

        function render() {
          const list = state.commands.filter(matches);
          grid.replaceChildren();
          empty.style.display = list.length ? 'none' : 'block';
          for (const c of list) {
            const card = document.createElement('div');
            card.className = 'card';
            // Configuration and examples are text, including literal <lang>/<text>.
            function field(className, text) {
              const node = document.createElement('div');
              node.className = className;
              node.textContent = text;
              card.appendChild(node);
            }
            field('name', '!' + c.name);
            field('roles', c.allowed_roles.join(', '));
            if (!c.implemented) field('badge', 'Unavailable');
            if (c.requirements) field('desc', c.requirements);
            if (c.description) field('desc', c.description);
            if (c.usage) field('usage', 'Usage: ' + c.usage);
            if (c.response) field('response', 'Configured response/example: ' + c.response);
            grid.appendChild(card);
          }
        }

        function validCatalog(value) {
          if (!value || !Array.isArray(value.commands)) return false;
          const names = new Set();
          return value.commands.every(c => {
            if (!c || typeof c.name !== 'string' || !c.name || names.has(c.name)) return false;
            names.add(c.name);
            return ['description', 'usage', 'response', 'requirements'].every(k => typeof c[k] === 'string') &&
              typeof c.implemented === 'boolean' && Array.isArray(c.allowed_roles) &&
              c.allowed_roles.every(r => typeof r === 'string');
          });
        }

        async function refetch() {
          try {
            const r = await fetch('commands.json', {cache:'no-store'});
            if (!r.ok) throw new Error('HTTP status');
            const j = await r.json();
            if (!validCatalog(j)) throw new Error('Catalog schema');
            const next = j.commands;
            const prev = state.commands;
            const changed = prev.length !== next.length ||
              prev.some((c, i) => !next[i] || next[i].name !== c.name || JSON.stringify(next[i]) !== JSON.stringify(c));
            if (changed) { state.commands = next; render(); }
            document.getElementById('status').textContent = 'Catalog loaded; availability reflects implemented dispatch, not live service readiness.';
          } catch (e) {
            document.getElementById('status').textContent = 'Catalog refresh failed. Showing the last successfully loaded catalog, if any.';
          }
        }

        refetch();
        setInterval(refetch, 5000);
      </script>
    </body>
    </html>
    """
    return html.replace("commands.json", "/api/commands") if api else html

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path.cwd() / "commands_site"))
    ap.add_argument("--watch", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    src = Path(__file__).resolve().parents[1] / "config" / "commands.yaml"
    last = 0
    build_once(out)
    if not args.watch:
        return
    while True:
        try:
            m = src.stat().st_mtime
            if m != last:
                last = m
                build_once(out)
        except Exception:
            pass
        time.sleep(2)

if __name__ == "__main__":
    main()