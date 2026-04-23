# PostgreSQL Query Reference

## Parameterised queries (always use these)

```sql
-- Good: no injection risk
SELECT * FROM orders WHERE user_id = $1 AND status = $2

-- Pass as: params: [42, "active"]
```

## Inspect patterns

```sql
-- Row counts for all tables in schema
SELECT schemaname, tablename, n_live_tup AS approx_rows
FROM pg_stat_user_tables
ORDER BY n_live_tup DESC;

-- Foreign key relationships
SELECT
    tc.table_name, kcu.column_name,
    ccu.table_name AS foreign_table,
    ccu.column_name AS foreign_column
FROM information_schema.table_constraints AS tc
JOIN information_schema.key_column_usage AS kcu
    ON tc.constraint_name = kcu.constraint_name
JOIN information_schema.constraint_column_usage AS ccu
    ON ccu.constraint_name = tc.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY';

-- Index list
SELECT indexname, tablename, indexdef
FROM pg_indexes
WHERE schemaname = $1
ORDER BY tablename, indexname;
```

## Aggregation patterns

```sql
-- Group with HAVING
SELECT department, COUNT(*) AS headcount
FROM employees
GROUP BY department
HAVING COUNT(*) > 5
ORDER BY headcount DESC;

-- Window function
SELECT id, amount,
       SUM(amount) OVER (PARTITION BY user_id ORDER BY created_at) AS running_total
FROM transactions
LIMIT 100;
```

## JSON / JSONB

```sql
-- Extract key
SELECT data->>'name' AS name FROM events WHERE data->>'type' = $1;

-- Array contains
SELECT * FROM products WHERE tags @> '["sale"]';
```

## Date ranges

```sql
SELECT * FROM logs
WHERE created_at BETWEEN NOW() - INTERVAL '7 days' AND NOW()
ORDER BY created_at DESC
LIMIT 500;
```
