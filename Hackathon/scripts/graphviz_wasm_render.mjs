import { Graphviz } from "@hpcc-js/wasm-graphviz";
import fs from "fs";
const g = await Graphviz.load();
const dot = fs.readFileSync(process.argv[2], "utf8");
fs.writeFileSync(process.argv[3], g.dot(dot, "svg"));
