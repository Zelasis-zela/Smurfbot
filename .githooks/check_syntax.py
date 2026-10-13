# Vérifie la syntaxe des fichiers Python sur le point d'être commités.
# Appelé par .githooks/pre-commit ; contrôle le contenu indexé (celui qui partira sur GitHub).
import subprocess
import sys

for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(errors="replace")

staged = subprocess.run(
    ["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"],
    capture_output=True, check=True,
).stdout.decode("utf-8").split("\0")

errors = []
for path in staged:
    if not path.endswith(".py"):
        continue
    source = subprocess.run(["git", "show", f":{path}"], capture_output=True, check=True).stdout
    try:
        compile(source, path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as e:
        line = getattr(e, "lineno", None)
        errors.append(f"  {path}" + (f", ligne {line}" if line else "") + f" : {getattr(e, 'msg', e)}")

if errors:
    print("Commit bloqué : erreur de syntaxe Python dans", file=sys.stderr)
    print("\n".join(errors), file=sys.stderr)
    print("Corrige le fichier, refais « git add », puis recommence le commit.", file=sys.stderr)
    sys.exit(1)
