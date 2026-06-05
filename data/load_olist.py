"""Loads Olist CSV files into a persistent DuckDB database at data/olist.duckdb."""

import re
from pathlib import Path

import duckdb

CSV_DIR = Path(__file__).parent / "olist"
DB_PATH = Path(__file__).parent / "olist.duckdb"


def clean_table_name(filename: str) -> str:
    name = Path(filename).stem
    name = re.sub(r"^olist_", "", name)
    name = re.sub(r"_dataset$", "", name)
    return name


def load(db_path: Path = DB_PATH, csv_dir: Path = CSV_DIR) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(db_path))

    for csv_file in sorted(csv_dir.glob("*.csv")):
        table = clean_table_name(csv_file.name)
        con.execute(f"DROP TABLE IF EXISTS {table}")
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_csv_auto('{csv_file}')")

        row_count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        columns = [row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()]
        print(f"{table}: {row_count} rows | {', '.join(columns)}")

    return con


if __name__ == "__main__":
    print(f"Loading CSVs from {CSV_DIR} into {DB_PATH}\n")
    con = load()
    con.close()
    print("\nDone.")
