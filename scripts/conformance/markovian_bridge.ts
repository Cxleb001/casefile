// TypeScript face of casefile.markovian (Python), so Meterless's own conformance runner can test it:
//
//   MARKOVIAN_IMPL=/abs/path/to/casefile/scripts/conformance/markovian_bridge.ts npx tsx runner.ts
//
// Exports what the runner requires (Markovian, modelTotals, runEfficiency). Every call is answered by
// the Python engine through markovian_bridge.py; the runner's generator functions are called from Python
// over a JSON-lines pipe, so the engine logic under test is the Python code, not a TypeScript copy.

import { execFileSync, spawn } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "..", "..");
const venvPython = path.join(repo, ".venv", "bin", "python");
const PYTHON = process.env.CASEFILE_PYTHON ?? (fs.existsSync(venvPython) ? venvPython : "python3");
const BRIDGE = path.join(here, "markovian_bridge.py");

function callSync(args: string[], input?: unknown): any {
  const out = execFileSync(PYTHON, [BRIDGE, ...args], { input: input === undefined ? "" : JSON.stringify(input) });
  return JSON.parse(out.toString().trim().split("\n").pop() as string);
}

export function modelTotals(N: number) {
  return callSync(["model_totals", String(N)]);
}

export function runEfficiency(chunks: unknown[], config: unknown) {
  return callSync(["run_efficiency"], { chunks, config });
}

type Generator = (prompt: string, attachments?: unknown[], systemPrompt?: string) => Promise<{ text: string }>;

export class Markovian {
  private config: Record<string, number>;
  private clock?: () => number;

  constructor(opts: { chunkConfig?: Record<string, number>; clock?: () => number } = {}) {
    const checked = callSync(["validate"], opts.chunkConfig ?? {});
    if (!checked.ok) throw new Error(checked.error); // typed rejection, e.g. out-of-range chunkSize
    this.config = opts.chunkConfig ?? {};
    this.clock = opts.clock;
  }

  run(options: { goal?: string; prompt?: string; generator: Generator; reflect?: boolean }): Promise<any> {
    return new Promise((resolve, reject) => {
      const child = spawn(PYTHON, [BRIDGE, "run"], { stdio: ["pipe", "pipe", "inherit"] });
      const lines = readline.createInterface({ input: child.stdout });
      let result: any;
      lines.on("line", async (line) => {
        const msg = JSON.parse(line);
        if (msg.type === "generate") {
          try {
            const out = await options.generator(msg.prompt, [], msg.system ?? undefined);
            child.stdin.write(JSON.stringify({ text: out.text }) + "\n");
          } catch (err) {
            child.stdin.write(JSON.stringify({ error: (err as Error).message }) + "\n");
          }
        } else if (msg.type === "result") {
          result = msg.run;
        }
      });
      child.on("close", (code) => (result ? resolve(result) : reject(new Error(`python bridge exited ${code}`))));
      child.stdin.write(JSON.stringify({ config: this.config, goal: options.goal ?? options.prompt,
        reflect: options.reflect ?? true, clock: this.clock ? this.clock() : undefined }) + "\n");
    });
  }
}
