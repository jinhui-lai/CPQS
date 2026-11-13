# Oracle
```sql
docker run -d -p 1521:1521 -e ORACLE_PWD=1213 --name oracle container-registry.oracle.com/database/free:latest
docker exec -it oracle /bin/bash
sqlplus / as sysdba
```

## [5537](https://forums.oracle.com/ords/apexds/post/select-min-c0-max-c0-performs-full-table-scan-instead-of-us-5537)-confirmed
<font style="color:rgb(22, 21, 19);background-color:rgb(251, 249, 248);">That's an interesting one...We're aware of this issue; there are existing enhancement requests related to this.</font>

```sql
-- Version 23.26.2.0.0
SET TIMING ON;
SET AUTOTRACE ON EXPLAIN;
-- insert into t0 with 100 million rows
CREATE TABLE t0(c0 NUMBER(10));
INSERT INTO t0 (c0) SELECT ROWNUM FROM XMLTABLE('1 to 100000000');
ALTER TABLE t0 ADD CONSTRAINT t0_pk PRIMARY KEY (c0);

-- positive cases
SELECT MAX(c0) FROM t0; -- INDEX FULL SCAN (MIN/MAX), Rows: 1
 MAX(C0)
----------
 100000000
Elapsed: 00:00:00.02
------------------------------------------------------------------------------------
| Id  | Operation                  | Name  | Rows  | Bytes | Cost (%CPU)| Time|
------------------------------------------------------------------------------------
|   0 | SELECT STATEMENT           |       |     1 |    13 |     3   (0)| 00:00:01 |
|   1 |  SORT AGGREGATE            |       |     1 |    13 |            | |
|   2 |   INDEX FULL SCAN (MIN/MAX)| T0_PK |     1 |    13 |     3   (0)| 00:00:01 |
------------------------------------------------------------------------------------

SELECT MIN(c0) FROM t0; -- INDEX FULL SCAN (MIN/MAX), Rows: 1
   MIN(C0)
----------
         1
Elapsed: 00:00:00.02
------------------------------------------------------------------------------------
| Id  | Operation                  | Name  | Rows  | Bytes | Cost (%CPU)| Time|
------------------------------------------------------------------------------------
|   0 | SELECT STATEMENT           |       |     1 |    13 |     3   (0)| 00:00:01 |
|   1 |  SORT AGGREGATE            |       |     1 |    13 |            | |
|   2 |   INDEX FULL SCAN (MIN/MAX)| T0_PK |     1 |    13 |     3   (0)| 00:00:01 |
------------------------------------------------------------------------------------

-- negative case
SELECT MAX(c0), MIN(c0) FROM t0;-- TABLE ACCESS FULL, Rows: 109M
   MAX(C0)    MIN(C0)
---------- ----------
 100000000          1
Elapsed: 00:00:03.56
---------------------------------------------------------------------------
| Id  | Operation          | Name | Rows  | Bytes | Cost (%CPU)| Time     |
---------------------------------------------------------------------------
|   0 | SELECT STATEMENT   |      |     1 |    13 | 41391   (1)| 00:00:02 |
|   1 |  SORT AGGREGATE    |      |     1 |    13 |            |          |
|   2 |   TABLE ACCESS FULL| T0   |   109M|  1357M| 41391   (1)| 00:00:02 |
---------------------------------------------------------------------------
```

## [8117](https://forums.oracle.com/ords/apexds/post/missed-scan-range-optimization-for-full-outer-join-when-pre-8117)-confirmed
```sql
-- Version 23.26.2.0.0
SET TIMING ON;
SET AUTOTRACE ON EXPLAIN;
-- insert into t0 with 1 million rows
CREATE TABLE t0(c0 NUMBER(10));
INSERT INTO t0 (c0) SELECT ROWNUM FROM XMLTABLE('1 to 1000');
CREATE TABLE t1(c0 NUMBER(10));
INSERT INTO t1 (c0) SELECT ROWNUM FROM XMLTABLE('1 to 1000000');
ALTER TABLE t0 ADD CONSTRAINT t0_pk PRIMARY KEY (c0);
ALTER TABLE t1 ADD CONSTRAINT t1_pk PRIMARY KEY (c0);
SELECT * FROM t0 FULL OUTER JOIN t1 ON t0.c0 = t1.c0 WHERE t0.c0 IN (1, 2, 3) OR t1.c0 IN (4, 5, 6);
----------------------------------------------------------------------------------
| Id  | Operation             | Name     | Rows  | Bytes | Cost (%CPU)| Time|
----------------------------------------------------------------------------------
|   0 | SELECT STATEMENT      |          |  1191K|    29M|  4528   (1)| 00:00:01|
|*  1 |  VIEW                 | VW_FOJ_0 |  1191K|    29M|  4528   (1)| 00:00:01|
|*  2 |   HASH JOIN FULL OUTER|          |  1191K|    29M|  4528   (1)| 00:00:01|
|   3 |    TABLE ACCESS FULL  | T0       |       |       |  4108   (1)| 00:00:01|
|   4 |    TABLE ACCESS FULL  | T1       |  1191K|    14M|   417   (1)| 00:00:01|
----------------------------------------------------------------------------------
Predicate Information (identified by operation id):
---------------------------------------------------
   1 - filter("T0"."C0"=1 OR "T0"."C0"=2 OR "T0"."C0"=3 OR "T1"."C0"=4 OR
              "T1"."C0"=5 OR "T1"."C0"=6)
   2 - access("T0"."C0"="T1"."C0")

SELECT * FROM t0 INNER JOIN t1 ON t0.c0 = t1.c0 WHERE t0.c0 IN (1, 2, 3) OR t1.c0 IN (4, 5, 6);
-----------------------------------------------------------------------------------------
| Id  | Operation             | Name            | Rows  | Bytes | Cost (%CPU)| Time     |
-----------------------------------------------------------------------------------------
|   0 | SELECT STATEMENT      |                 |     2 |    52 |   176   (0)| 00:00:01 |
|   1 |  VIEW                 | VW_ORE_D628D811 |     2 |    52 |   176   (0)| 00:00:01 |
|   2 |   UNION-ALL           |                 |     2 |    52 |   176   (0)| 00:00:01 |
|   3 |    NESTED LOOPS       |                 |     1 |    26 |    93   (0)| 00:00:01 |
|   4 |     INLIST ITERATOR   |                 |       |       |            |        |
|*  5 |      INDEX UNIQUE SCAN| T0_PK           |     1 |    13 |    92   (0)| 00:00:01 |
|*  6 |     INDEX UNIQUE SCAN | T1_PK           |     1 |    13 |     1   (0)| 00:00:01 |
|   7 |    NESTED LOOPS       |                 |     1 |    26 |    83   (0)| 00:00:01 |
|   8 |     INLIST ITERATOR   |                 |       |       |            |        |
|*  9 |      INDEX UNIQUE SCAN| T1_PK           |    72 |   936 |    11   (0)| 00:00:01 |
|* 10 |     INDEX UNIQUE SCAN | T0_PK           |     1 |    13 |     1   (0)| 00:00:01 |
-----------------------------------------------------------------------------------------
Predicate Information (identified by operation id):
---------------------------------------------------
   5 - access("T0"."C0"=1 OR "T0"."C0"=2 OR "T0"."C0"=3)
   6 - access("T0"."C0"="T1"."C0")
   9 - access("T1"."C0"=4 OR "T1"."C0"=5 OR "T1"."C0"=6)
  10 - access("T0"."C0"="T1"."C0")
       filter(LNNVL("T0"."C0"=1) AND LNNVL("T0"."C0"=2) AND LNNVL("T0"."C0"=3))       
```

## [1074](https://forums.oracle.com/ords/apexds/post/redundant-order-by-after-unique-pk-disables-ordered-fetch-s-1074)
```sql
CREATE TABLE t0 (c0 NUMBER(10) NOT NULL, part NUMBER(10) NOT NULL, PRIMARY KEY (c0));
INSERT /*+ APPEND */ INTO t0 (c0, part) SELECT LEVEL, LEVEL FROM dual CONNECT BY LEVEL <= 1000000;
COMMIT;
BEGIN DBMS_STATS.GATHER_TABLE_STATS(USER, 'T0'); END;
/

-- (A) expected: PK index ordered stopkey
SELECT c0 FROM t0 ORDER BY c0 FETCH FIRST 1 ROW ONLY;

-- EXPLAIN PLAN SET STATEMENT_ID = 'A' FOR SELECT c0 FROM t0 ORDER BY c0 FETCH FIRST 1 ROW ONLY
/*
Plan hash value: 259805217

----------------------------------------------------------------------------------
| Id  | Operation         | Name         | Rows  | Bytes | Cost (%CPU)| Time     |
----------------------------------------------------------------------------------
|   0 | SELECT STATEMENT  |              |     1 |     5 |     3   (0)| 00:00:01 |
|*  1 |  COUNT STOPKEY    |              |       |       |            |          |
|   2 |   VIEW            |              |     1 |     5 |     3   (0)| 00:00:01 |
|   3 |    INDEX FULL SCAN| SYS_C0027830 |  1000K|  4882K|     3   (0)| 00:00:01 |
----------------------------------------------------------------------------------

Predicate Information (identified by operation id):
---------------------------------------------------

   1 - filter(ROWNUM<=1)
*/


-- (B) redundant ORDER BY part — should match (A)
SELECT c0 FROM t0 ORDER BY c0, part FETCH FIRST 1 ROW ONLY;

-- EXPLAIN PLAN SET STATEMENT_ID = 'B' FOR SELECT c0 FROM t0 ORDER BY c0, part FETCH FIRST 1 ROW ONLY
/*
Plan hash value: 3777931950

----------------------------------------------------------------------------------------
| Id  | Operation               | Name | Rows  | Bytes |TempSpc| Cost (%CPU)| Time     |
----------------------------------------------------------------------------------------
|   0 | SELECT STATEMENT        |      |     1 |     5 |       |  4556   (1)| 00:00:01 |
|*  1 |  COUNT STOPKEY          |      |       |       |       |            |          |
|   2 |   VIEW                  |      |  1000K|  4882K|       |  4556   (1)| 00:00:01 |
|*  3 |    SORT ORDER BY STOPKEY|      |  1000K|  9765K|    19M|  4556   (1)| 00:00:01 |
|   4 |     TABLE ACCESS FULL   | T0   |  1000K|  9765K|       |   566   (1)| 00:00:01 |
----------------------------------------------------------------------------------------

Predicate Information (identified by operation id):
---------------------------------------------------

   1 - filter(ROWNUM<=1)
   3 - filter(ROWNUM<=1)
*/

```

```sql
-- positive case
SELECT c0 FROM t1 ORDER BY c0 DESC LIMIT 1 OFFSET 1;
SELECT c0 FROM t1 ORDER BY c0 ASC LIMIT 1 OFFSET 1;
```

## To Report
```sql
SET TIMING ON;
SET AUTOTRACE ON EXPLAIN;
-- insert into t0 with 100 million rows
CREATE TABLE t0(c0 NUMBER(10));
CREATE TABLE t1(c0 NUMBER(10));
INSERT INTO t0 (c0) SELECT ROWNUM FROM XMLTABLE('1 to 1000000');
INSERT INTO t1 (c0) SELECT ROWNUM FROM XMLTABLE('1 to 1000');
ALTER TABLE t0 ADD CONSTRAINT t0_pk PRIMARY KEY (c0);
ALTER TABLE t1 ADD CONSTRAINT t1_pk PRIMARY KEY (c0);

SELECT * FROM t0, t1 WHERE (t0.c0, t1.c0) IN ((0,0),(1,1)); -- positive case

SELECT MAX(t0.c0), MAX(t1.c0) FROM t0, t1;
MAX(T0.C0) MAX(T1.C0)
---------- ----------
   1000000       1000
Elapsed: 00:00:40.28
------------------------------------------------------------------------------
| Id  | Operation             | Name | Rows  | Bytes | Cost (%CPU)| Time     |
------------------------------------------------------------------------------
|   0 | SELECT STATEMENT      |      |     1 |    26 |   390K  (1)| 00:00:16 |
|   1 |  SORT AGGREGATE       |      |     1 |    26 |            |          |
|   2 |   MERGE JOIN CARTESIAN|      |   715M|    17G|   390K  (1)| 00:00:16 |
|   3 |    TABLE ACCESS FULL  | T0   |   715K|  9079K|   415   (1)| 00:00:01 |
|   4 |    BUFFER SORT        |      |  1000 | 13000 |   389K  (1)| 00:00:16 |
|   5 |     TABLE ACCESS FULL | T1   |  1000 | 13000 |     1   (0)| 00:00:01 |
------------------------------------------------------------------------------
```

# MySQL
## [120477](https://bugs.mysql.com/bug.php?id=120477)-confirmed
```sql
-- Index Not Used for HAVING Without GROUP BY or Aggregate Functions
DELIMITER //
CREATE PROCEDURE batch_insert_numbers()
BEGIN
    DECLARE i INT DEFAULT 1;
    DECLARE batch_size INT DEFAULT 1000;
    
    WHILE i <= 1000000 DO
        START TRANSACTION;
        WHILE i <= 1000000 AND batch_size > 0 DO
            INSERT INTO t0(c0) VALUES (i);
            SET i = i + 1;
            SET batch_size = batch_size - 1;
        END WHILE;
        COMMIT;
        SET batch_size = 1000;
    END WHILE;
END //
DELIMITER ;

CREATE TABLE t0(c0 INT PRIMARY KEY);
CALL batch_insert_numbers(); -- insert into t0(c0) with values: 1-1000000

SELECT * FROM t0 WHERE t0.c0 = 1; -- result: {1|0}; time: 0.001 sec

EXPLAIN SELECT * FROM t0 WHERE t0.c0 = 1 \G
*************************** 1. row ***************************
EXPLAIN: -> Rows fetched before execution  (cost=0..0 rows=1)

SELECT * FROM t0 HAVING t0.c0 = 1;  -- result: {1|0}; time: 0.124 sec

EXPLAIN SELECT * FROM t0 HAVING t0.c0 = 1 \G
*************************** 1. row ***************************
EXPLAIN: -> Filter: (t0.c0 = 0)  (cost=100290 rows=998968)
    -> Table scan on t0  (cost=100290 rows=998968)
```

## [120490](https://bugs.mysql.com/bug.php?id=120490)-confirmed
```sql
-- Join Not Eliminated When Constant Table is Empty Due to IN (NULL) Condition
SET SESSION range_alloc_block_size = 1024*1024*16; -- 16 MB; default value is 4096, i.e., 4 KB
CREATE TABLE t0(c0 FLOAT PRIMARY KEY);
CREATE TABLE t1(c0 FLOAT);

DELIMITER //
CREATE PROCEDURE batch_insert_numbers()
BEGIN
    DECLARE i INT DEFAULT 1;
    DECLARE batch_size INT DEFAULT 1000;
    
    WHILE i <= 1000000 DO
        START TRANSACTION;
        WHILE i <= 1000000 AND batch_size > 0 DO
            INSERT INTO t0 VALUES (i);
            INSERT INTO t1 VALUES (i);
            SET i = i + 1;
            SET batch_size = batch_size - 1;
        END WHILE;
        COMMIT;
        SET batch_size = 1000;
    END WHILE;
END //
DELIMITER ;

-- Insert into t0 and t1 with 1,000,000 rows via procedure
CALL batch_insert_numbers();

explain analyze SELECT * FROM t0, t1 WHERE t0.c0 > t1.c0 AND t0.c0 IN (NULL) AND t0.c0 = 0 \G
*************************** 1. row ***************************
EXPLAIN: -> Nested loop inner join  (cost=200277 rows=998585) (actual time=491..491 rows=0 loops=1)
    -> Table scan on t1  (cost=100419 rows=998585) (actual time=0.069..265 rows=1e+6 loops=1)
    -> Filter: (('0' > t1.c0) and ('0' = NULL) and ('0' = 0))  (cost=0.35 rows=1) (actual time=148e-6..148e-6 rows=0 loops=1e+6)
        -> Constant row from t0  (cost=0.35 rows=1) (actual time=48.4e-6..48.4e-6 rows=0 loops=1e+6)
1 row in set, 1 warning (0.508 sec)

explain analyze SELECT * FROM t0, t1 WHERE t0.c0 > t1.c0 AND t0.c0 IS NULL AND t0.c0 = 0 \G
*************************** 1. row ***************************
EXPLAIN: -> Zero rows (Impossible WHERE)  (cost=0..0 rows=0) (actual time=60e-6..60e-6 rows=0 loops=1)
1 row in set (0.001 sec)

explain analyze SELECT * FROM t0, t1 WHERE t1.c0 > t0.c0 AND t0.c0 = 0 \G
*************************** 1. row ***************************
EXPLAIN: -> Zero rows (no matching row in const table)  (cost=0..0 rows=0) (actual time=80e-6..80e-6 rows=0 loops=1)
1 row in set (0.001 sec)

-- similar case
explain analyze SELECT * FROM t0, t1 WHERE t0.c0 > t1.c0 AND t0.c0 IS UNKNOWN AND t0.c0 = 0;
```

## [120495](https://bugs.mysql.com/bug.php?id=120495)-confirmed
```sql
CREATE TABLE t0(c0 INT, c1 DOUBLE);

DELIMITER //
CREATE PROCEDURE batch_insert_numbers()
BEGIN
    DECLARE i INT DEFAULT 1;
    DECLARE batch_size INT DEFAULT 1000;
    
    WHILE i <= 1000000 DO
        START TRANSACTION;
        WHILE i <= 1000000 AND batch_size > 0 DO
            INSERT INTO t0 VALUES (i, i);
            SET i = i + 1;
            SET batch_size = batch_size - 1;
        END WHILE;
        COMMIT;
        SET batch_size = 1000;
    END WHILE;
END //
DELIMITER ;
CALL batch_insert_numbers();
CREATE INDEX i0 ON t0(c1);
CREATE INDEX i1 ON t0(c0, c1);
EXPLAIN SELECT * FROM t0 WHERE (t0.c0 OR t0.c1) AND t0.c1 = 1 \G
*************************** 1. row ***************************
EXPLAIN: -> Filter: ((0 <> t0.c0) or (0 <> t0.c1))  (cost=52452 rows=498961)
    -> Index lookup on t0 using i0 (c1 = 1)  (cost=52452 rows=498961)

DROP INDEX i1 ON t0;
EXPLAIN SELECT * FROM t0 WHERE ((t0.c0) OR (t0.c1)) AND t0.c1 = 1 \G
*************************** 1. row ***************************
EXPLAIN: -> Filter: ((0 <> t0.c0) or (0 <> t0.c1))  (cost=0.445 rows=1)
    -> Index lookup on t0 using i0 (c1 = 1)  (cost=0.445 rows=1)
```

## [120502](https://bugs.mysql.com/bug.php?id=120502)-confirmed
```sql
CREATE TABLE t0(c0 INT);

DELIMITER //
CREATE PROCEDURE batch_insert_numbers()
BEGIN
    DECLARE i INT DEFAULT 1;
    DECLARE batch_size INT DEFAULT 1000;
    
    WHILE i <= 1000000 DO
        START TRANSACTION;
        WHILE i <= 1000000 AND batch_size > 0 DO
            INSERT INTO t0 VALUES (i);
            SET i = i + 1;
            SET batch_size = batch_size - 1;
        END WHILE;
        COMMIT;
        SET batch_size = 1000;
    END WHILE;
END //
DELIMITER ;

CALL batch_insert_numbers();

-- positive cases
explain analyze select * from t0 where t0.c0 > 0 AND t0.c0 = 0 \G
explain analyze select * from t0 where t0.c0 < 0 AND t0.c0 = 0 \G
explain analyze select * from t0 where t0.c0 <> 0 AND t0.c0 = 0 \G
*************************** 1. row ***************************
EXPLAIN: -> Zero rows (Impossible WHERE)  (cost=0..0 rows=0) (actual time=80e-6..80e-6 rows=0 loops=1)
1 row in set (0.001 sec)

-- missed optimization 
explain analyze select * from t0 where t0.c0 > 0 AND t0.c0 < 0 \G
*************************** 1. row ***************************
EXPLAIN: -> Filter: ((t0.c0 > 0) and (t0.c0 < 0))  (cost=100419 rows=110932) (actual time=293..293 rows=0 loops=1)
    -> Table scan on t0  (cost=100419 rows=998585) (actual time=0.0279..247 rows=1e+6 loops=1)
1 row in set (0.294 sec)

explain analyze select * from t0 where t0.c0 >= 0 AND t0.c0 < 0 \G
*************************** 1. row ***************************
EXPLAIN: -> Filter: ((t0.c0 >= 0) and (t0.c0 < 0))  (cost=100419 rows=110932) (actual time=300..300 rows=0 loops=1)
    -> Table scan on t0  (cost=100419 rows=998585) (actual time=0.113..254 rows=1e+6 loops=1)
1 row in set (0.301 sec)

explain analyze select * from t0 where t0.c0 > 0 AND t0.c0 <= 0 \G
*************************** 1. row ***************************
EXPLAIN: -> Filter: ((t0.c0 > 0) and (t0.c0 <= 0))  (cost=100419 rows=110932) (actual time=292..292 rows=0 loops=1)
    -> Table scan on t0  (cost=100419 rows=998585) (actual time=0.0289..245 rows=1e+6 loops=1)
1 row in set (0.292 sec)
```

## [120522](https://bugs.mysql.com/bug.php?id=120522)-confirmed
```sql
CREATE TABLE t0(c0 INT PRIMARY KEY);
CREATE TABLE t1(c0 INT PRIMARY KEY);
DELIMITER //
CREATE PROCEDURE batch_insert_numbers()
BEGIN
    DECLARE i INT DEFAULT 1;
    DECLARE batch_size INT DEFAULT 1000;
    
    WHILE i <= 1000000 DO
        START TRANSACTION;
        WHILE i <= 1000000 AND batch_size > 0 DO
            INSERT INTO t0 VALUES (i);
            INSERT INTO t1 VALUES (i);
            SET i = i + 1;
            SET batch_size = batch_size - 1;
        END WHILE;
        COMMIT;
        SET batch_size = 1000;
    END WHILE;
END //
DELIMITER ;

-- Insert into t0 and t1 with 1,000,000 rows via procedure
CALL batch_insert_numbers();

EXPLAIN ANALYZE SELECT * FROM t0, t1 WHERE (t0.c0, t1.c0) IN ((1, 1), (2, 2)) \G
*************************** 1. row ***************************
EXPLAIN: -> Filter: ((t0.c0,t1.c0) in ((1,1),(2,2)))  (cost=200178 rows=399600) (actual time=2.28..307 rows=2 loops=1)
    -> Inner hash join (no condition)  (cost=200178 rows=399600) (actual time=2.28..201 rows=2e+6 loops=1)
        -> Covering index scan on t1 using PRIMARY  (cost=10178 rows=999000) (actual time=2.23..111 rows=1e+6 loops=1)
        -> Hash
            -> Covering index range scan on t0 using PRIMARY over (c0 = 1) OR (c0 = 2)  (cost=0.91 rows=2) (actual time=0.0333..0.0361 rows=2 loops=1)
1 row in set (0.308 sec)

EXPLAIN ANALYZE SELECT * FROM t0, t1 WHERE (t0.c0 = 1 AND t1.c0 = 1) OR (t0.c0 = 2 AND t1.c0 = 2) \G
*************************** 1. row ***************************
EXPLAIN: -> Filter: (((t1.c0 = 1) and (t0.c0 = 1)) or ((t1.c0 = 2) and (t0.c0 = 2)))  (cost=2.73 rows=4) (actual time=0.0551..0.058 rows=2 loops=1)
    -> Inner hash join (no condition)  (cost=2.73 rows=4) (actual time=0.0541..0.0566 rows=4 loops=1)
        -> Covering index range scan on t1 using PRIMARY over (c0 = 1) OR (c0 = 2)  (cost=0.81 rows=2) (actual time=0.00776..0.00937 rows=2 loops=1)
        -> Hash
            -> Filter: ((t0.c0 = 1) or (t0.c0 = 2))  (cost=0.91 rows=2) (actual time=0.0349..0.0384 rows=2 loops=1)
                -> Covering index range scan on t0 using PRIMARY over (c0 = 1) OR (c0 = 2)  (cost=0.91 rows=2) (actual time=0.0333..0.0363 rows=2 loops=1)
1 row in set (0.001 sec)
```

## [120666](https://bugs.mysql.com/bug.php?id=120666)-confirmed
Thank you for the bug report. We consider this to be a reasonable request for optimization.

```sql
DELIMITER //
CREATE PROCEDURE batch_insert_numbers()
BEGIN
    DECLARE i INT DEFAULT 1;
    DECLARE batch_size INT DEFAULT 1000;
    
    WHILE i <= 1000000 DO
        START TRANSACTION;
        WHILE i <= 1000000 AND batch_size > 0 DO
            INSERT INTO t0(c0) VALUES (i);
            SET i = i + 1;
            SET batch_size = batch_size - 1;
        END WHILE;
        COMMIT;
        SET batch_size = 1000;
    END WHILE;
END //
DELIMITER ;

CREATE TABLE t0(c0 INT PRIMARY KEY);
CALL batch_insert_numbers();

EXPLAIN SELECT c0 FROM t0 WHERE ABS(t0.c0)<10 \G
*************************** 1. row ***************************
EXPLAIN: -> Filter: (abs(t0.c0) < 10)  (cost=100277 rows=999000)
    -> Covering index scan on t0 using PRIMARY  (cost=100277 rows=999000)

EXPLAIN SELECT c0 FROM t0 WHERE t0.c0 > -10 AND t0.c0 < 10 \G
*************************** 1. row ***************************
EXPLAIN: -> Filter: ((t0.c0 > <cache>(-(10))) and (t0.c0 < 10))  (cost=2.06 rows=9)
    -> Covering index range scan on t0 using PRIMARY over (-10 < c0 < 10)  (cost=2.06 rows=9)
```

## [120683](https://bugs.mysql.com/bug.php?id=120683)-confirmed
```sql
DELIMITER //
CREATE PROCEDURE batch_insert_numbers()
BEGIN
    DECLARE i INT DEFAULT 1;
    DECLARE batch_size INT DEFAULT 1000;
    
    WHILE i <= 1000000 DO
        START TRANSACTION;
        WHILE i <= 1000000 AND batch_size > 0 DO
            INSERT INTO t0(c0) VALUES (i);
            SET i = i + 1;
            SET batch_size = batch_size - 1;
        END WHILE;
        COMMIT;
        SET batch_size = 1000;
    END WHILE;
END //
DELIMITER ;

CREATE TABLE t0(c0 INT PRIMARY KEY);
CALL batch_insert_numbers();

EXPLAIN SELECT * FROM t0 LIMIT 1 \G
*************************** 1. row ***************************
EXPLAIN: -> Limit: 1 row(s)  (cost=100277 rows=1)
    -> Covering index scan on t0 using PRIMARY  (cost=100277 rows=999000)

1 row in set (0.001 sec)

EXPLAIN SELECT * FROM t0 ORDER BY t0.c0 ASC LIMIT 1 \G
*************************** 1. row ***************************
EXPLAIN: -> Limit: 1 row(s)  (cost=377e-6 rows=1)
    -> Covering index scan on t0 using PRIMARY  (cost=377e-6 rows=1)
```

## [121648](https://bugs.mysql.com/bug.php?id=121648)-confirmed
```sql
CREATE TABLE t0 (c0 INT NOT NULL, part INT NOT NULL, PRIMARY KEY (c0));
DELIMITER //
CREATE PROCEDURE batch_insert_numbers()
BEGIN
    DECLARE i INT DEFAULT 1;
    DECLARE batch_size INT DEFAULT 1000;
    
    WHILE i <= 1000000 DO
        START TRANSACTION;
        WHILE i <= 1000000 AND batch_size > 0 DO
            INSERT INTO t0 VALUES (i, i);
            SET i = i + 1;
            SET batch_size = batch_size - 1;
        END WHILE;
        COMMIT;
        SET batch_size = 1000;
    END WHILE;
END //
DELIMITER ;

-- Insert into t0 with 1,000,000 rows via procedure
CALL batch_insert_numbers();

ANALYZE TABLE t0;

-- (A) good: PRIMARY reverse index scan, stop after 1 row
SELECT c0 FROM t0 ORDER BY c0 DESC LIMIT 1;

-- EXPLAIN ANALYZE FORMAT=TREE SELECT c0 FROM t0 ORDER BY c0 DESC LIMIT 1
/*
-> Limit: 1 row(s)  (cost=350e-9 rows=1) (actual time=0.0211..0.0211 rows=1 loops=1)
    -> Covering index scan on t0 using PRIMARY (reverse)  (cost=350e-9 rows=1) (actual time=0.0196..0.0196 rows=1 loops=1)
*/

-- (B) bad: redundant ORDER BY part — should match (A)
SELECT c0 FROM t0 ORDER BY c0 DESC, part DESC LIMIT 1;

-- EXPLAIN ANALYZE FORMAT=TREE SELECT c0 FROM t0 ORDER BY c0 DESC, part DESC LIMIT 1
/*
-> Limit: 1 row(s)  (cost=100000 rows=1) (actual time=206..206 rows=1 loops=1)
    -> Sort: t0.c0 DESC, t0.part DESC, limit input to 1 row(s) per chunk  (cost=100000 rows=1e+6) (actual time=206..206 rows=1 loops=1)
        -> Table scan on t0  (cost=100000 rows=1e+6) (actual time=1.33..138 rows=1e+6 loops=1)
*/

```

# MariaDB
## [39964](https://jira.mariadb.org/browse/MDEV-39964)-confirmed
```sql
-- Index Not Used for HAVING Without GROUP BY or Aggregate Functions
CREATE TABLE t0(c0 INT8 PRIMARY KEY);
INSERT INTO t0 SELECT seq FROM seq_1_to_1000000;

SELECT * FROM t0 WHERE t0.c0 = 1; -- 0.000 sec
SELECT * FROM t0 HAVING t0.c0 = 1; -- 0.085 sec

EXPLAIN SELECT * FROM t0 WHERE t0.c0 = 1;
+------+-------------+-------+-------+---------------+---------+---------+-------+------+-------+
| id   | select_type | table | type  | possible_keys | key     | key_len | ref   | rows | Extra |
+------+-------------+-------+-------+---------------+---------+---------+-------+------+-------+
|    1 | SIMPLE      | t0    | const | PRIMARY       | PRIMARY | 8       | const | 1    |       |
+------+-------------+-------+-------+---------------+---------+---------+-------+------+-------+

EXPLAIN SELECT * FROM t0 HAVING t0.c0 = 1;
+------+-------------+-------+------+---------------+------+---------+------+--------+-------+
| id   | select_type | table | type | possible_keys | key  | key_len | ref  | rows   | Extra |
+------+-------------+-------+------+---------------+------+---------+------+--------+-------+
|    1 | SIMPLE      | t0    | ALL  | NULL          | NULL | NULL    | NULL | 998739 |       |
+------+-------------+-------+------+---------------+------+---------+------+--------+-------+
```

## [39975](https://jira.mariadb.org/browse/MDEV-39975)-confirmed
```sql
CREATE TABLE t0(c0 INT8);
INSERT INTO t0 SELECT seq FROM seq_1_to_1000000;

-- positive cases
SELECT * FROM t0 WHERE t0.c0 > 0 AND t0.c0 = 0; -- Empty set (0.001 sec)
SELECT * FROM t0 WHERE t0.c0 < 0 AND t0.c0 = 0; -- Empty set (0.001 sec)
SELECT * FROM t0 WHERE t0.c0 <> 0 AND t0.c0 = 0; -- Empty set (0.001 sec)
-- This plan uses Impossible WHERE
+------+-------------+-------+------+---------------+------+---------+------+------+------------------+
| id   | select_type | table | type | possible_keys | key  | key_len | ref  | rows | Extra    |
+------+-------------+-------+------+---------------+------+---------+------+------+------------------+
|    1 | SIMPLE      | NULL  | NULL | NULL          | NULL | NULL    | NULL | NULL | Impossible WHERE |
+------+-------------+-------+------+---------------+------+---------+------+------+------------------+

-- negative cases
SELECT * FROM t0 WHERE t0.c0 > 0 AND t0.c0 < 0; -- Empty set (0.442 sec)
SELECT * FROM t0 WHERE t0.c0 >= 0 AND t0.c0 < 0; -- Empty set (0.442 sec)
SELECT * FROM t0 WHERE t0.c0 > 0 AND t0.c0 <= 0; -- Empty set (0.442 sec)
-- This plan performs a full table scan
+------+-------------+-------+------+---------------+------+---------+------+--------+-------------+
| id   | select_type | table | type | possible_keys | key  | key_len | ref  | rows   | Extra |
+------+-------------+-------+------+---------------+------+---------+------+--------+-------------+
|    1 | SIMPLE      | t0    | ALL  | NULL          | NULL | NULL    | NULL | 998412 | Using where |
+------+-------------+-------+------+---------------+------+---------+------+--------+-------------+
```

## [40020](https://jira.mariadb.org/browse/MDEV-40020)-confirmed
```sql
CREATE TABLE t0(c0 INT PRIMARY KEY);
INSERT INTO t0 SELECT seq FROM seq_1_to_1000000;

-- positive case
EXPLAIN SELECT * FROM t0 WHERE t0.c0 > -10 AND t0.c0 < 10;
+------+-------------+-------+-------+---------------+---------+---------+------+------+-------------+
| id   | select_type | table | type  | possible_keys | key     | key_len | ref  | rows | Extra       |
+------+-------------+-------+-------+---------------+---------+---------+------+------+-------------+
|    1 | SIMPLE      | t0    | range | PRIMARY       | PRIMARY | 4       | NULL | 9    | Using where |
+------+-------------+-------+-------+---------------+---------+---------+------+------+-------------+

-- negative case
EXPLAIN SELECT * FROM t0 WHERE ABS(t0.c0) < 10;
+------+-------------+-------+------+---------------+------+---------+------+--------+-------------+
| id   | select_type | table | type | possible_keys | key  | key_len | ref  | rows   | Extra       |
+------+-------------+-------+------+---------------+------+---------+------+--------+-------------+
|    1 | SIMPLE      | t0    | ALL  | NULL          | NULL | NULL    | NULL | 999000 | Using where |
+------+-------------+-------+------+---------------+------+---------+------+--------+-------------+
```

## [40034](https://jira.mariadb.org/browse/MDEV-40034)-confirmed
```sql
CREATE TABLE t0(c0 INT PRIMARY KEY);
INSERT INTO t0 SELECT seq FROM seq_1_to_1000000;

EXPLAIN SELECT * FROM t0 LIMIT 1;
+------+-------------+-------+------+---------------+------+---------+------+--------+-------+
| id   | select_type | table | type | possible_keys | key  | key_len | ref  | rows   | Extra |
+------+-------------+-------+------+---------------+------+---------+------+--------+-------+
|    1 | SIMPLE      | t0    | ALL  | NULL          | NULL | NULL    | NULL | 999000 |       |
+------+-------------+-------+------+---------------+------+---------+------+--------+-------+

EXPLAIN SELECT * FROM t0 ORDER BY t0.c0 ASC LIMIT 1;
+------+-------------+-------+-------+---------------+---------+---------+------+------+-------+
| id   | select_type | table | type  | possible_keys | key     | key_len | ref  | rows | Extra |
+------+-------------+-------+-------+---------------+---------+---------+------+------+-------+
|    1 | SIMPLE      | t0    | index | NULL          | PRIMARY | 4       | NULL | 1    |       |
+------+-------------+-------+-------+---------------+---------+---------+------+------+-------+
```

## [41993](https://jira.mariadb.org/browse/MDEV-41993)
```sql
CREATE TABLE t0 (c0 INT NOT NULL, part INT NOT NULL, PRIMARY KEY (c0));
INSERT INTO t0 SELECT seq, seq FROM seq_1_to_1000000;

-- (A) expected: PRIMARY index reverse scan, stop after 1 row
ANALYZE FORMAT=JSON SELECT c0 FROM t0 ORDER BY c0 DESC LIMIT 1
/*
{
  "query_block": {
    "select_id": 1,
    "r_total_time_ms": 0.023515057,
    "nested_loop": [
      {
        "table": {
          "table_name": "t0",
          "access_type": "index",
          "key": "PRIMARY",
          "used_key_parts": ["c0"],
          "rows": 998739,
          "r_rows": 1,
          "r_table_time_ms": 0.014215601,
          "r_engine_stats": { "pages_accessed": 3 }
        }
      }
    ]
  }
}
*/

-- (B) redundant ORDER BY part — should match (A)
ANALYZE FORMAT=JSON SELECT c0 FROM t0 ORDER BY c0 DESC, part DESC LIMIT 1
/*
{
  "query_block": {
    "select_id": 1,
    "r_total_time_ms": 116.5682576,
    "nested_loop": [
      {
        "read_sorted_file": {
          "r_rows": 1,
          "filesort": {
            "sort_key": "t0.c0 desc, t0.part desc",
            "r_total_time_ms": 116.5325276,
            "r_limit": 1,
            "r_used_priority_queue": true,
            "table": {
              "table_name": "t0",
              "access_type": "ALL",
              "rows": 998739,
              "r_rows": 1000000,
              "r_table_time_ms": 73.01364779,
              "r_engine_stats": { "pages_accessed": 1748 }
            }
          }
        }
      }
    ]
  }
}
*/
```

# TiDB
```sql
create table t1 (a1 int, b1 int, index bx(b1))
explain select 1 from t1 where b1 in (1,2);
+--------------------------+---------+-----------+------------------------+----------------------------------------------------+
| id                       | estRows | task      | access object          | operator info                                      |
+--------------------------+---------+-----------+------------------------+----------------------------------------------------+
| Projection_4             | 20.00   | root      |                        | 1->Column#4                                        |
| └─IndexReader_7          | 20.00   | root      |                        | index:IndexRangeScan_6                             |
|   └─IndexRangeScan_6     | 20.00   | cop[tikv] | table:t1, index:bx(b1) | range:[1,1], [2,2], keep order:false, stats:pseudo |
+--------------------------+---------+-----------+------------------------+----------------------------------------------------+

explain select 1 from t1 where b1=1 or b1=2
+--------------------------+---------+-----------+------------------------+---------------------------------------------+
| id                       | estRows | task      | access object          | operator info                               |
+--------------------------+---------+-----------+------------------------+---------------------------------------------+
| Projection_4             | 250.00  | root      |                        | 1->Column#4                                 |
| └─IndexReader_7          | 250.00  | root      |                        | index:IndexRangeScan_6                      |
|   └─IndexRangeScan_6     | 250.00  | cop[tikv] | table:t1, index:bx(b1) | range:[1,2], keep order:false, stats:pseudo |
+--------------------------+---------+-----------+------------------------+---------------------------------------------+
```

## [69058](https://github.com/pingcap/tidb/issues/69058)-confirmed-fixed
```sql
CREATE TEMPORARY TABLE digits (d INT);
INSERT INTO digits VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9);

-- insert into t0 with values 0-999999
CREATE TABLE t0(c0 INT);
INSERT INTO t0
SELECT d1.d + d2.d*10 + d3.d*100 + d4.d*1000 + d5.d*10000 + d6.d*100000 AS num
  FROM digits d1, digits d2, digits d3, digits d4, digits d5, digits d6
  WHERE d1.d + d2.d*10 + d3.d*100 + d4.d*1000 + d5.d*10000 + d6.d*100000 <= 1000000;

-- positive cases
EXPLAIN ANALYZE SELECT * FROM t0 WHERE t0.c0 > 0 AND t0.c0 = 0 \G
EXPLAIN ANALYZE SELECT * FROM t0 WHERE t0.c0 < 0 AND t0.c0 = 0 \G
EXPLAIN ANALYZE SELECT * FROM t0 WHERE t0.c0 <> 0 AND t0.c0 = 0 \G
*************************** 1. row ***************************
            id: TableDual_5
       estRows: 0.00
       actRows: 0
1 row in set (0.00 sec)

-- missed optimization
EXPLAIN ANALYZE SELECT * FROM t0 WHERE t0.c0 > 0 AND t0.c0 < 0 \G
EXPLAIN ANALYZE SELECT * FROM t0 WHERE t0.c0 > 0 AND t0.c0 <= 0 \G
EXPLAIN ANALYZE SELECT * FROM t0 WHERE t0.c0 >= 0 AND t0.c0 < 0 \G
*************************** 1. row ***************************
            id: TableReader_7
       estRows: 0.00
       actRows: 0
          task: root
 access object: 
execution info: ...
 operator info: data:Selection_6
*************************** 2. row ***************************
            id: └─Selection_6
       estRows: 0.00
       actRows: 0
          task: cop[tikv]
 access object: 
execution info:  ...
 operator info: gt(test.t0.c0, 0), lt(test.t0.c0, 0); gt(test.t0.c0, 0), le(test.t0.c0, 0); ge(test.t0.c0, 0), lt(test.t0.c0, 0)
*************************** 3. row ***************************
            id:   └─TableFullScan_5
       estRows: 1000000.00
       actRows: 1000000
          task: cop[tikv]
 access object: table:t0
execution info:  ...
 operator info: keep order:false
3 rows in set (0.28 sec)
```

## [69158](https://github.com/pingcap/tidb/issues/69158)-confirmed
```sql
CREATE TEMPORARY TABLE digits (d INT);
INSERT INTO digits VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9);

CREATE TABLE t0(c0 INT);
INSERT INTO t0
SELECT d1.d + d2.d*10 + d3.d*100 + d4.d*1000 + d5.d*10000 + d6.d*100000 AS num
  FROM digits d1, digits d2, digits d3, digits d4, digits d5, digits d6
  WHERE d1.d + d2.d*10 + d3.d*100 + d4.d*1000 + d5.d*10000 + d6.d*100000 <= 1000000;

EXPLAIN SELECT c0 FROM t0 WHERE ABS(t0.c0)<10;
+-------------------------+------------+-----------+---------------+-------------------------+
| id                      | estRows    | task      | access object | operator info           |
+-------------------------+------------+-----------+---------------+-------------------------+
| TableReader_7           | 800000.00  | root      |               | data:Selection_6        |
| └─Selection_6           | 800000.00  | cop[tikv] |               | lt(abs(test.t0.c0), 10) |
|   └─TableFullScan_5     | 1000000.00 | cop[tikv] | table:t0      | keep order:false        |
+-------------------------+------------+-----------+---------------+-------------------------+

EXPLAIN SELECT c0 FROM t0 WHERE t0.c0>-10 AND t0.c0<10;
+------------------------+---------+-----------+---------------+----------------------------------+
| id                     | estRows | task      | access object | operator info                    |
+------------------------+---------+-----------+---------------+----------------------------------+
| TableReader_6          | 0.00    | root      |               | data:TableRangeScan_5            |
| └─TableRangeScan_5     | 0.00    | cop[tikv] | table:t0      | range:(-10,10), keep order:false |
+------------------------+---------+-----------+---------------+----------------------------------+
```

## [69164](https://github.com/pingcap/tidb/issues/69164)-confirmed
```sql
CREATE TEMPORARY TABLE digits (d INT);
INSERT INTO digits VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9);

CREATE TABLE t0(c0 INT PRIMARY KEY);
INSERT INTO t0
SELECT d1.d + d2.d*10 + d3.d*100 + d4.d*1000 + d5.d*10000 + d6.d*100000 AS num
  FROM digits d1, digits d2, digits d3, digits d4, digits d5, digits d6
  WHERE d1.d + d2.d*10 + d3.d*100 + d4.d*1000 + d5.d*10000 + d6.d*100000 <= 1000000;

CREATE TABLE t1 LIKE t0;
INSERT INTO t1 SELECT * FROM t0 LIMIT 1000;

-- positive case
SELECT MAX(t0.c0) FROM t0; -- 0.00 sec
EXPLAIN SELECT MAX(t0.c0) FROM t0;
+------------------------------+---------+-----------+---------------+---------------------------------+
| id                           | estRows | task      | access object | operator info                   |
+------------------------------+---------+-----------+---------------+---------------------------------+
| StreamAgg_9                  | 1.00    | root      |               | funcs:max(test.t0.c0)->Column#2 |
| └─Limit_13                   | 1.00    | root      |               | offset:0, count:1               |
|   └─TableReader_20           | 1.00    | root      |               | data:Limit_19                   |
|     └─Limit_19               | 1.00    | cop[tikv] |               | offset:0, count:1               |
|       └─TableFullScan_18     | 1.00    | cop[tikv] | table:t0      | keep order:true, desc           |
+------------------------------+---------+-----------+---------------+---------------------------------+

-- negative case
SELECT MAX(t0.c0), MAX(t1.c0) FROM t0, t1; -- 24.37 sec
EXPLAIN SELECT MAX(t0.c0), MAX(t1.c0) FROM t0, t1;
+---------------------------------+----------------+-----------+---------------+------------------------------------------------------------------+
| id                              | estRows        | task      | access object | operator info                                                    |
+---------------------------------+----------------+-----------+---------------+------------------------------------------------------------------+
| HashAgg_9                       | 1.00           | root      |               | funcs:max(test.t0.c0)->Column#3, funcs:max(test.t1.c0)->Column#4 |
| └─Projection_11                 | 10000000000.00 | root      |               | test.t0.c0, test.t1.c0                        |
|   └─HashJoin_13                 | 10000000000.00 | root      |               | CARTESIAN inner join                        |
|     ├─TableReader_15(Build)     | 10000.00       | root      |               | data:TableFullScan_14                        |
|     │ └─TableFullScan_14        | 10000.00       | cop[tikv] | table:t1      | keep order:false, stats:pseudo                        |
|     └─TableReader_17(Probe)     | 1000000.00     | root      |               | data:TableFullScan_16                        |
|       └─TableFullScan_16        | 1000000.00     | cop[tikv] | table:t0      | keep order:false                        |
+---------------------------------+----------------+-----------+---------------+------------------------------------------------------------------+
```

## [72585](https://github.com/pingcap/tidb/issues/72585)-confirmed
```sql
CREATE TABLE t0 (c0 INT PRIMARY KEY, c1 INT NOT NULL);

CREATE TEMPORARY TABLE digits (d INT);
INSERT INTO digits VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9);

INSERT INTO t0 (c0, c1)
SELECT 
    num AS c0,
    num AS c1
FROM (
    SELECT d1.d + d2.d*10 + d3.d*100 + d4.d*1000 + d5.d*10000 + d6.d*100000 AS num
    FROM digits d1, digits d2, digits d3, digits d4, digits d5, digits d6
    WHERE d1.d + d2.d*10 + d3.d*100 + d4.d*1000 + d5.d*10000 + d6.d*100000 <= 1000000
) AS numbers;

-- (A) expected: PK ordered limit
SELECT c0 FROM t0 ORDER BY c0 LIMIT 1;

-- EXPLAIN ANALYZE SELECT c0 FROM t0 ORDER BY c0 LIMIT 1
/*
Limit_11 | 1.00 | 1 | root |  | time:254.7µs | offset:0, count:1
└─TableReader_18 | 1.00 | 1 | root |  | time:252µs | data:Limit_17
  └─Limit_17 | 1.00 | 1 | cop[tikv] |  | tikv_task:{time:167.9µs} | offset:0, count:1
    └─TableFullScan_16 | 1.00 | 64 | cop[tikv] | table:t0 | keep order:true, stats:pseudo
*/

-- (B) redundant ORDER BY c1 — should match (A)
SELECT c0 FROM t0 ORDER BY c0, c1 LIMIT 1;

-- EXPLAIN ANALYZE SELECT c0 FROM t0 ORDER BY c0, c1 LIMIT 1
/*
Projection_7 | 1.00 | 1 | root |  | time:446.8ms | cpqs.t0.c0
└─TopN_8 | 1.00 | 1 | root |  | time:446.8ms | cpqs.t0.c0, cpqs.t0.c1, offset:0, count:1
  └─TableReader_16 | 1.00 | 1 | root |  | time:446.8ms | data:TopN_15
    └─TopN_15 | 1.00 | 1000000 | cop[tikv] |  | tikv_task:{time:446.6ms} | cpqs.t0.c0, cpqs.t0.c1, offset:0, count:1
      └─TableFullScan_14 | 10000.00 | 1000000 | cop[tikv] | table:t0 | keep order:false, stats:pseudo
*/
```



```sql
CREATE TABLE t0(c0 INT, c1 INT, c2 INT, PRIMARY KEY(c0, c1));
CREATE TABLE t1 LIKE t0;

CREATE TEMPORARY TABLE digits (d INT);
INSERT INTO digits VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9);

INSERT INTO t0 (c0, c1)
SELECT 
    num AS c0,
    num AS c1
FROM (
    SELECT d1.d + d2.d*10 + d3.d*100 + d4.d*1000 + d5.d*10000 + d6.d*100000 AS num
    FROM digits d1, digits d2, digits d3, digits d4, digits d5, digits d6
    WHERE d1.d + d2.d*10 + d3.d*100 + d4.d*1000 + d5.d*10000 + d6.d*100000 <= 1000000
) AS numbers;

INSERT INTO t1 SELECT * FROM t0;
```

# CockroachDB
## [170885](https://github.com/cockroachdb/cockroach/issues/170885)-confirmed
<font style="color:rgb(31, 35, 40);">Thanks for the report </font>[<font style="color:rgb(31, 35, 40);">@jinhui-lai</font>](https://github.com/jinhui-lai)<font style="color:rgb(31, 35, 40);">, I'm able to reproduce this on master. We might need a normalization rule that expands multi-element tuple IN into an OR of ANDs (sorta like SimplifyInSingleElement).</font>

```sql
CREATE TABLE t0 (c0 INT PRIMARY KEY);
CREATE TABLE t1 (c0 INT PRIMARY KEY);
INSERT INTO t0 SELECT * FROM generate_series(1, 1000000);
INSERT INTO t1 SELECT * FROM generate_series(1, 1000);

EXPLAIN ANALYZE SELECT * FROM t0, t1 WHERE (t0.c0, t1.c0) IN ((0,0),(1,1));
-- Full cross join, 1M × 1K rows, ~80s

EXPLAIN ANALYZE SELECT * FROM t0, t1 WHERE (t0.c0=0 AND t1.c0=0) OR (t0.c0=1 AND t1.c0=1);
-- Union of two cross joins with point scans, ~2ms
```

## [171603](https://github.com/cockroachdb/cockroach/issues/171603)-confirmed
<font style="color:rgb(31, 35, 40);">Nice idea!</font>

```sql
CREATE TABLE t0 (c0 INT PRIMARY KEY);
INSERT INTO t0 SELECT * FROM generate_series(1, 1000000);

-- positive case
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 > -10 AND t0.c0 < 10;;                        
  distribution: local
  vectorized: true
  • scan
    columns: (c0)
    estimated row count: 19 (missing stats)
    table: t0@t0_pkey
    spans: /-9-/10
    parallel

-- negative case
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE ABS(t0.c0) < 10;                                    
  distribution: local
  vectorized: true
  • filter
  │ columns: (c0)
  │ estimated row count: 333 (missing stats)
  │ filter: abs(c0) < 10
  │
  └── • scan
        columns: (c0)
        estimated row count: 1,000 (missing stats)
        table: t0@t0_pkey
        spans: FULL SCAN
```

## [171626](https://github.com/cockroachdb/cockroach/issues/171626)-confirmed
```sql
CREATE TABLE t0 (c0 INT PRIMARY KEY);
CREATE TABLE t1 (c0 INT PRIMARY KEY);
INSERT INTO t0 SELECT * FROM generate_series(1, 1000000);
INSERT INTO t1 SELECT * FROM generate_series(1, 1000);

-- Since t0 is a PRIMARY KEY, MAX(t0.c0) can be obtained via LIMITED SCAN
SELECT MAX(t0.c0) FROM t0; -- 2ms
-- The optimizer cannot push the LIMIT through the CROSS JOIN, forcing FULL SCAN
SELECT MAX(t0.c0), MAX(t1.c0) FROM t0, t1; -- 3.796s

EXPLAIN SELECT MAX(t0.c0) FROM t0;                                                           
  distribution: local
  vectorized: true
  • group (scalar)
  │
  └── • revscan
        missing stats
        table: t0@t0_pkey
        spans: LIMITED SCAN
        limit: 1

EXPLAIN SELECT MAX(t0.c0), Max(t1.c0) FROM t0, t1;
  distribution: local
  vectorized: true
  • render
  │
  └── • group (scalar)
      │
      └── • cross join
          │
          ├── • scan
          │     missing stats
          │     table: t0@t0_pkey
          │     spans: FULL SCAN
          │
          └── • scan
                missing stats
                table: t1@t1_pkey
                spans: FULL SCAN
```

## [175448](https://github.com/cockroachdb/cockroach/issues/175448)-confirmed
```sql
CREATE TABLE t0 (c0 INT PRIMARY KEY);
CREATE TABLE t1 (c0 INT PRIMARY KEY);
INSERT INTO t0 SELECT * FROM generate_series(1, 1000000);
INSERT INTO t1 SELECT * FROM generate_series(1, 1000);
EXPLAIN SELECT * FROM t0 FULL OUTER JOIN t1 ON t0.c0 = t1.c0 WHERE t0.c0 IN (1, 2, 3) OR t1.c0 IN (4, 5, 6);
  • filter
  │ filter: (c0 IN (1, 2, 3)) OR (c0 IN (4, 5, 6))
  │
  └── • merge join (full outer)
      │ equality: (c0) = (c0)
      │ left cols are key
      │ right cols are key
      │
      ├── • scan
      │     missing stats
      │     table: t0@t0_pkey
      │     spans: FULL SCAN
      │
      └── • scan
            missing stats
            table: t1@t1_pkey
            spans: FULL SCAN
```

[175603](https://github.com/cockroachdb/cockroach/issues/175603)-duplicate of 175448

```sql
-- CockroachDB
CREATE TABLE t0 (c0 INT PRIMARY KEY);
CREATE TABLE t1 (c0 INT PRIMARY KEY);
INSERT INTO t0 SELECT * FROM generate_series(1, 1000000);
INSERT INTO t1 SELECT * FROM generate_series(1, 1000000);
EXPLAIN SELECT * FROM t0 LEFT OUTER JOIN t1 ON ((t0.c0 = 1 AND t1.c0 = 1) OR (t0.c0 = 2 AND t1.c0 = 2)) WHERE t0.c0 IN (1, 2);
• cross join (right outer)
│ pred: ((c0 = 1) AND (c0 = 1)) OR ((c0 = 2) AND (c0 = 2))
│
├── • scan
│     missing stats
│     table: t1@t1_pkey
│     spans: FULL SCAN
│
└── • scan
      missing stats
      table: t0@t0_pkey
      spans: [/1 - /2]
        
-- PostgreSQL            
EXPLAIN SELECT * FROM t0 LEFT OUTER JOIN t1 ON ((t0.c0 = 1 AND t1.c0 = 1) OR (t0.c0 = 2 AND t1.c0 = 2)) WHERE t0.c0 IN (1, 2);
Nested Loop Left Join  (cost=0.68..21.45 rows=1 width=16)
 Join Filter: (((t0.c0 = 1) AND (t1.c0 = 1)) OR ((t0.c0 = 2) AND (t1.c0 = 2)))
 ->  Index Only Scan using t0_pkey on t0  (cost=0.25..12.53 rows=1 width=8)
       Index Cond: (c0 = ANY ('{1,2}'::bigint[]))
 ->  Index Only Scan using t1_pkey on t1  (cost=0.42..8.88 rows=2 width=8)
       Index Cond: (c0 = ANY ('{1,2}'::integer[]))

-- MySQL
EXPLAIN SELECT * FROM t0 LEFT OUTER JOIN t1 ON ((t0.c0 = 1 AND t1.c0 = 1) OR (t0.c0 = 2 AND t1.c0 = 2)) WHERE t0.c0 IN (1, 2);
 -> Left hash join (no condition), extra conditions: (((t1.c0 = 1) and (t0.c0 = 1)) or ((t1.c0 = 2) and (t0.c0 = 2)))  (cost=2.63 rows=4)
    -> Filter: (t0.c0 in (1,2))  (cost=0.91 rows=2)
        -> Covering index range scan on t0 using PRIMARY over (c0 = 1) OR (c0 = 2)  (cost=0.91 rows=2)
    -> Hash
        -> Covering index range scan on t1 using PRIMARY over (c0 = 1) OR (c0 = 2)  (cost=0.81 rows=2)

-- MariaDB
EXPLAIN SELECT * FROM t0 LEFT OUTER JOIN t1 ON ((t0.c0 = 1 AND t1.c0 = 1) OR (t0.c0 = 2 AND t1.c0 = 2)) WHERE t0.c0 IN (1, 2);
+------+-------------+-------+-------+---------------+---------+---------+------+------+-------------------------------------------------+
| id   | select_type | table | type  | possible_keys | key     | key_len | ref  | rows | Extra                                       |
+------+-------------+-------+-------+---------------+---------+---------+------+------+-------------------------------------------------+
|    1 | SIMPLE      | t0    | range | PRIMARY       | PRIMARY | 4       | NULL | 2    | Using where                                     |
|    1 | SIMPLE      | t1    | range | PRIMARY       | PRIMARY | 4       | NULL | 2    | Using where; Using join buffer (flat, BNL join) |
+------+-------------+-------+-------+---------------+---------+---------+------+------+-------------------------------------------------+
```



```sql
CREATE TABLE t0 (c0 INT PRIMARY KEY);
CREATE TABLE t1 (c0 INT PRIMARY KEY);
INSERT INTO t0 SELECT * FROM generate_series(1, 1000000);
INSERT INTO t1 SELECT * FROM generate_series(1, 1000);

CREATE TABLE t0 (c0 INT);
CREATE TABLE t1 (c0 INT);
INSERT INTO t0 SELECT * FROM generate_series(1, 1000000);
INSERT INTO t1 SELECT * FROM generate_series(1, 1000);

EXPLAIN SELECT * FROM t0, t1 WHERE t0.c0=t1.c0 AND t1.c0 IN (0,1);
```



```sql
-- docker exec -it roach1 ./cockroach sql --insecure
CREATE TABLE t0(c0 Int64, c1 Int64);
INSERT INTO t0 SELECT *,* FROM generate_series(1, 1000000);
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 != t0.c0;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 > t0.c0;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 < t0.c0;

-- negative cases
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 = t0.c0 + 1;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 = t0.c0 - 1;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 > t0.c0 + 100;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 < t0.c0 - 100;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 >= t0.c0 + 1;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 <= t0.c0 - 1;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 * 0 = 1;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 % 1 = 1;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 ^ 2 < 0;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 * t0.c0 < 0;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE ABS(t0.c0) < 0;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 > (SELECT 1 WHERE FALSE); 
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 < (SELECT 1 WHERE FALSE);
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 != (SELECT 1 WHERE FALSE);
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE NULLIF(t0.c0, t0.c0) = 1;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE GREATEST(t0.c0, t0.c0 + 1) = LEAST(t0.c0, t0.c0);

-- positive cases
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 = NULL;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 != NULL;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 > NULL;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 IN (NULL);
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 NOT IN (NULL);
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE 1 = 0;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE FALSE;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE TRUE AND FALSE;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE NOT (1 = 1);
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 BETWEEN 10 AND 5;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 = 10 AND t0.c0 = 20;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 = 10 AND t0.c0 != 10;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 IN (SELECT c0 FROM t0 WHERE 1 = 0);
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 = ANY (ARRAY[]::int[]);
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 = SOME (ARRAY[1]) AND t0.c0 = 2;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE EXISTS (SELECT 1 FROM t0 WHERE FALSE);
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 = (SELECT 1 WHERE FALSE);
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 = (SELECT NULL);
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE t0.c0 IN (SELECT 1 WHERE FALSE);
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE 1 = 0 AND t0.c0 = 1;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE CASE WHEN 1 = 0 THEN t0.c0 END = 1;
EXPLAIN (VERBOSE) SELECT * FROM t0 WHERE COALESCE(NULL, NULL) = 1;
```