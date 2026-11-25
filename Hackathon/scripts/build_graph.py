"""graphify build ./src ./knowledge_vault --output graph.json  (run from repo root)"""
import runpy, sys
sys.path.insert(0, "src")
sys.argv = ["graphify", "build", "./src", "./knowledge_vault", "--output", "graph.json"]
runpy.run_module("unreliable.graphify", run_name="__main__")
