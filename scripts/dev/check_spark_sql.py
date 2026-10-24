"""Dev check: run the agent workup through real Spark SQL (the engine family under Zetaris Lightning)
and compare every decision with the DuckDB stand-in. Needs Java 17+ and `pip install pyspark==3.5.3`.

    python scripts/dev/check_spark_sql.py ALR-001 ALR-009
"""
import sys, time, csv, os, decimal, datetime, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("JAVA_TOOL_OPTIONS", "")
from pyspark.sql import SparkSession
from casefile.config import get_settings, PREPARED_DIR, EVAL_DIR
from casefile.data.base import QueryResult
from casefile.data.schema import KYC_SHARED_COLUMNS

opens = " ".join(f"--add-opens=java.base/{p}=ALL-UNNAMED" for p in
                 ["java.lang", "java.lang.invoke", "java.lang.reflect", "java.io", "java.net", "java.nio", "java.util",
                  "java.util.concurrent", "java.util.concurrent.atomic", "sun.nio.ch", "sun.nio.cs", "sun.security.action", "sun.util.calendar"])
spark = (SparkSession.builder.master("local[2]").appName("casefile-sql-check")
         .config("spark.driver.extraJavaOptions", opens + " -Djava.security.manager=allow")
         .config("spark.sql.shuffle.partitions", "4").config("spark.ui.enabled", "false")
         .config("spark.sql.warehouse.dir", str(Path(tempfile.mkdtemp()) / "warehouse"))
         .getOrCreate())
spark.sparkContext.setLogLevel("ERROR")
for db in ["inst_a", "inst_b", "shared", "compliance"]:
    spark.sql(f"CREATE DATABASE IF NOT EXISTS {db}")
P = PREPARED_DIR
spark.sql(f"CREATE TABLE IF NOT EXISTS inst_a.ledger_a USING parquet OPTIONS (path '{P}/ledger_a.parquet')")
spark.sql(f"CREATE TABLE IF NOT EXISTS inst_a.kyc_a USING parquet OPTIONS (path '{P}/kyc_a.parquet')")
spark.sql(f"CREATE TABLE IF NOT EXISTS inst_b.ledger_b USING parquet OPTIONS (path '{P}/ledger_b.parquet')")
spark.sql(f"CREATE TABLE IF NOT EXISTS inst_b.kyc_b USING parquet OPTIONS (path '{P}/kyc_b.parquet')")
spark.sql(f"CREATE TABLE IF NOT EXISTS compliance.watchlist USING csv OPTIONS (path '{P}/watchlist.csv', header 'true', inferSchema 'true')")
spark.sql(f"CREATE OR REPLACE VIEW shared.kyc_b AS SELECT {', '.join(KYC_SHARED_COLUMNS)} FROM inst_b.kyc_b")

def conv(v):
    if isinstance(v, decimal.Decimal): return float(v)
    if isinstance(v, datetime.date) and not isinstance(v, datetime.datetime): return datetime.datetime(v.year, v.month, v.day)
    return v

class SparkBackend:
    name = "spark-sql"
    def query(self, sql, columns=None, limit=None):
        t = time.perf_counter()
        df = spark.sql(sql)
        rows = [[conv(v) for v in r] for r in df.limit(limit or 5000).collect()]
        names = list(columns) if columns and len(columns) == len(df.columns) else df.columns
        return QueryResult(sql=sql, columns=names, rows=rows, backend=self.name, duration_ms=int((time.perf_counter()-t)*1000))
    def close(self): pass

from casefile.evidence import EvidenceStore
from casefile.agent.case import CaseState
from casefile.agent.tools import Toolbox
from casefile.agent.workup import standard_workup
from casefile.agent.report import rules_report
from casefile.data.local import LocalBackend

settings = get_settings()
out = tempfile.mkdtemp()
store = EvidenceStore(out + "/ev.duckdb")
alerts = list(csv.DictReader(open(EVAL_DIR / "alerts.csv")))
ids = sys.argv[1:] or [a["alert_id"] for a in alerts[:6]]
spark_b, duck_b = SparkBackend(), LocalBackend(settings)
errors = 0
for al in [a for a in alerts if a["alert_id"] in ids]:
    res = {}
    for name, be in (("spark", spark_b), ("duckdb", duck_b)):
        case = CaseState(al["alert_id"] + "-" + name, al)
        tb = Toolbox(be, store, settings, case)
        t = time.time(); obs = standard_workup(tb); dt = time.time() - t
        errs = [(o.tool, o.error) for o in obs if o.error]
        errors += len(errs) if name == "spark" else 0
        rep = rules_report(case)
        res[name] = (rep["disposition"], rep["typology"], len(case.edges), sorted((s["name"], s["strength"]) for s in case.signals), errs, round(dt, 1))
    same = res["spark"][:4] == res["duckdb"][:4]
    print(al["alert_id"], "MATCH" if same else "DIFF", "spark:", res["spark"][0], res["spark"][1], res["spark"][2], "edges", f"{res['spark'][5]}s", "| duckdb:", res["duckdb"][0], res["duckdb"][1], res["duckdb"][2], "edges")
    if res["spark"][4]: print("   SPARK ERRORS:", res["spark"][4])
    if not same: print("   spark signals", res["spark"][3], "\n   duck  signals", res["duckdb"][3])
print("spark tool errors:", errors)
