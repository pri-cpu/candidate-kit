# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
Unique employees.emp_code; unique attendance_logs(emp_code, date) for race-safe punch-in, per-employee queries and the trend. (date desc, emp_code) serves the list sort and leaderboard month scan; (status, date, emp_code) the status filter; (emp_code, punch_in desc) finds the record to punch out. Rejected a separate index on late_minutes: queries filter by month first
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
Both requests compute the same date and call insert_one. The unique index lets exactly one succeed; the other gets DuplicateKeyError, which I turn into 409. There is no check before the insert.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
The leaderboard uses $rank, so ties share a rank (1, 2, 2, 4). The limit filters on rank, so everyone tied at the cutoff is returned, ordered by emp_code.
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
The summary starts from employees (joined on or before month end) and left-joins logs, keeping people with none. Headcount is the distinct employees per department.
5. **One thing you would change** if this had to serve 100x the data.
Pre-aggregate monthly totals per employee, so analytics don't scan raw logs.
