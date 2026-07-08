"""Backfill script: clear corrupted task titles that are raw XML fragments.

Run once after deploying fix #56 to remove any titles that start with '<'
(i.e. titles that were set from XML-structured content before the
_is_valid_title guard was added).

Usage:
    python scripts/backfill_task_titles.py
"""
import duckdb

con = duckdb.connect('/Users/arnabmac/.nexus/nexus.duckdb')
result = con.execute("UPDATE tasks SET title = NULL WHERE title LIKE '<%'").fetchone()
print(f'Cleared corrupted titles')
con.close()
