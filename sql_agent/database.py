"""
Database Module
===============
PostgreSQL database management and operations.
"""

import re
import logging
import time
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

from .config import Config
from .security import SqlPolicy, validate_sql

# Setup logger
logger = logging.getLogger(__name__)

class DatabaseManager:
    """Manages PostgreSQL database connections and operations."""

    # Cap on returned rows. Enforced twice: injected as a LIMIT into the SQL by
    # the AST guard (so the server never materialises more) and again by
    # fetchmany (so a LIMIT the guard could not rewrite still cannot flood us).
    MAX_ROWS = 500

    # Predefined schema for the face detection system
    KNOWN_SCHEMA = {
        "tables": {
            "faces": {
                "description": "Detected and recognized faces with bounding box coordinates",
                "columns": [
                    {"column_name": "id", "data_type": "integer", "is_nullable": "NO", "description": "Primary key"},
                    {"column_name": "detection_id", "data_type": "integer", "is_nullable": "NO", "description": "Foreign key to detections table"},
                    {"column_name": "name", "data_type": "varchar", "is_nullable": "YES", "description": "Recognized person's name"},
                    {"column_name": "similarity", "data_type": "float", "is_nullable": "YES", "description": "Face recognition similarity score (0-1)"},
                    {"column_name": "bbox_x1", "data_type": "integer", "is_nullable": "YES", "description": "Bounding box top-left X coordinate"},
                    {"column_name": "bbox_y1", "data_type": "integer", "is_nullable": "YES", "description": "Bounding box top-left Y coordinate"},
                    {"column_name": "bbox_x2", "data_type": "integer", "is_nullable": "YES", "description": "Bounding box bottom-right X coordinate"},
                    {"column_name": "bbox_y2", "data_type": "integer", "is_nullable": "YES", "description": "Bounding box bottom-right Y coordinate"},
                ],
                "primary_keys": ["id"],
                "foreign_keys": [{"column_name": "detection_id", "foreign_table_name": "detections", "foreign_column_name": "id"}]
            },
            "detections": {
                "description": "Detection events from the face detection pipeline",
                "columns": [
                    {"column_name": "id", "data_type": "integer", "is_nullable": "NO", "description": "Primary key"},
                    {"column_name": "pipeline_id", "data_type": "varchar", "is_nullable": "NO", "description": "Camera/pipeline identifier (varchar) - matches pipelines.pipeline_id, NOT pipelines.id"},
                    {"column_name": "timestamp", "data_type": "timestamp", "is_nullable": "NO", "description": "When the detection occurred"},
                    {"column_name": "uuid", "data_type": "varchar", "is_nullable": "NO", "description": "Unique identifier for the detection"},
                ],
                "primary_keys": ["id"],
                "foreign_keys": [{"column_name": "pipeline_id", "foreign_table_name": "pipelines", "foreign_column_name": "pipeline_id"}]
            },
            "pipelines": {
                "description": "Camera/pipeline configurations tracking which camera captured each detection",
                "columns": [
                    {"column_name": "id", "data_type": "integer", "is_nullable": "NO", "description": "Primary key"},
                    {"column_name": "pipeline_id", "data_type": "varchar", "is_nullable": "NO", "description": "Opaque camera identifier (often a UUID) used as the join key to detections.pipeline_id. NOT human-readable — never show it to a user when location_name is available"},
                    {"column_name": "location_name", "data_type": "varchar", "is_nullable": "YES", "description": "The HUMAN-READABLE camera name (e.g. 'Main Entrance', 'Parking Lot'). Reports must display COALESCE(location_name, pipeline_id) AS camera_name so a camera without a configured name still identifies itself"},
                    {"column_name": "latitude", "data_type": "double precision", "is_nullable": "YES", "description": "Camera latitude (WGS84), when configured"},
                    {"column_name": "longitude", "data_type": "double precision", "is_nullable": "YES", "description": "Camera longitude (WGS84), when configured"},
                    {"column_name": "is_active", "data_type": "boolean", "is_nullable": "NO", "description": "Whether this camera/pipeline is currently active"},
                    {"column_name": "created_at", "data_type": "timestamp", "is_nullable": "NO", "description": "When the camera/pipeline was created"},
                    {"column_name": "updated_at", "data_type": "timestamp", "is_nullable": "YES", "description": "Last update timestamp"},
                    {"column_name": "total_detections", "data_type": "integer", "is_nullable": "YES", "description": "Total number of detections by this camera"},
                ],
                "primary_keys": ["id"],
                "foreign_keys": []
            },
            "system_metrics": {
                "description": "System performance metrics and resource usage",
                "columns": [
                    {"column_name": "id", "data_type": "integer", "is_nullable": "NO", "description": "Primary key"},
                    {"column_name": "timestamp", "data_type": "timestamp", "is_nullable": "NO", "description": "When the metric was recorded"},
                    {"column_name": "queue_size", "data_type": "integer", "is_nullable": "YES", "description": "Current processing queue size"},
                    {"column_name": "processing_count", "data_type": "integer", "is_nullable": "YES", "description": "Number of items being processed"},
                    {"column_name": "cpu_percent", "data_type": "float", "is_nullable": "YES", "description": "CPU usage percentage"},
                    {"column_name": "memory_percent", "data_type": "float", "is_nullable": "YES", "description": "Memory usage percentage"},
                ],
                "primary_keys": ["id"],
                "foreign_keys": []
            }
        },
        "relationships": [
            "faces.detection_id → detections.id (Many faces can belong to one detection event)",
            "detections.pipeline_id → pipelines.pipeline_id (varchar to varchar - Many detections belong to one camera/pipeline)",
            "Complete chain: faces → detections → pipelines allows tracking which camera detected which person",
            "IMPORTANT: detections.pipeline_id is VARCHAR and joins to pipelines.pipeline_id (VARCHAR), NOT pipelines.id (INTEGER)"
        ]
    }

    def __init__(self, config: Config):
        self.config = config
        self._schema_cache: Optional[dict] = None
        self._use_known_schema: bool = True  # Set to False to fetch schema dynamically

        # The allowlist is derived from the schema the agent is told about, so
        # the two cannot drift: a table the model was never shown is a table it
        # may not read. This is what prevents `SELECT ... FROM users`.
        self.sql_policy = SqlPolicy.for_tables(
            self.KNOWN_SCHEMA["tables"].keys(),
            dialect="postgres",
            max_rows=self.MAX_ROWS,
        )

    def get_connection(self):
        """Create a new database connection."""
        try:
            logger.debug(f"[DB] Connecting to database: {self.config.db_name}@{self.config.db_host}:{self.config.db_port}")
            conn = psycopg2.connect(
                host=self.config.db_host,
                port=self.config.db_port,
                dbname=self.config.db_name,
                user=self.config.db_user,
                password=self.config.db_password,
                sslmode="disable",
                connect_timeout=5,
                # Server-side hardening: any LLM-generated query is killed by
                # Postgres after 30s, and the whole session is read-only at the
                # database level (defense in depth beyond _validate_query).
                options="-c statement_timeout=30000 -c default_transaction_read_only=on",
            )
            logger.debug("[DB] Database connection established successfully")
            return conn  # Return the OPEN connection
        except Exception as e:
            logger.error(f"[DB] Connection failed: {type(e).__name__}: {str(e)}", exc_info=True)
            logger.error("❌ CONNECTION ERROR:")
            logger.info(type(e).__name__)
            logger.info(e)
            raise  # Re-raise the exception so caller knows connection failed

    def fetch_schema(self) -> dict:
        """Fetch and cache the database schema."""
        if self._schema_cache:
            return self._schema_cache

        schema = {"tables": {}}

        try:
            with self.get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    # Get all tables
                    cur.execute("""
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                        AND table_type = 'BASE TABLE'
                    """)
                    tables = cur.fetchall()

                    for table in tables:
                        table_name = table['table_name']

                        # Get columns for each table
                        cur.execute("""
                            SELECT
                                column_name,
                                data_type,
                                is_nullable,
                                column_default,
                                character_maximum_length
                            FROM information_schema.columns
                            WHERE table_schema = 'public'
                            AND table_name = %s
                            ORDER BY ordinal_position
                        """, (table_name,))
                        columns = cur.fetchall()

                        # Get primary keys
                        cur.execute("""
                            SELECT a.attname as column_name
                            FROM pg_index i
                            JOIN pg_attribute a ON a.attrelid = i.indrelid
                                AND a.attnum = ANY(i.indkey)
                            WHERE i.indrelid = %s::regclass
                            AND i.indisprimary
                        """, (table_name,))
                        primary_keys = [row['column_name'] for row in cur.fetchall()]

                        # Get foreign keys
                        cur.execute("""
                            SELECT
                                kcu.column_name,
                                ccu.table_name AS foreign_table_name,
                                ccu.column_name AS foreign_column_name
                            FROM information_schema.table_constraints AS tc
                            JOIN information_schema.key_column_usage AS kcu
                                ON tc.constraint_name = kcu.constraint_name
                            JOIN information_schema.constraint_column_usage AS ccu
                                ON ccu.constraint_name = tc.constraint_name
                            WHERE tc.constraint_type = 'FOREIGN KEY'
                            AND tc.table_name = %s
                        """, (table_name,))
                        foreign_keys = cur.fetchall()

                        schema["tables"][table_name] = {
                            "columns": columns,
                            "primary_keys": primary_keys,
                            "foreign_keys": foreign_keys
                        }

            self._schema_cache = schema

        except Exception as e:
            schema["error"] = str(e)

        return schema

    def get_schema_description(self) -> str:
        """Get a human-readable schema description for the LLM."""
        # Use known schema if enabled, otherwise fetch dynamically
        if self._use_known_schema:
            schema = self.KNOWN_SCHEMA
        else:
            schema = self.fetch_schema()
            if "error" in schema:
                return f"Error fetching schema: {schema['error']}"

        lines = [
            "DATABASE SCHEMA - Face Detection System",
            "=" * 50,
            "",
            "This database stores face detection and recognition data from video pipelines.",
            ""
        ]

        for table_name, table_info in schema["tables"].items():
            desc = table_info.get("description", "")
            lines.append(f"TABLE: {table_name}")
            if desc:
                lines.append(f"  Description: {desc}")
            lines.append("-" * 40)

            # Columns
            lines.append("  Columns:")
            for col in table_info["columns"]:
                nullable = "NULL" if col["is_nullable"] == "YES" else "NOT NULL"
                pk_marker = " [PK]" if col["column_name"] in table_info.get("primary_keys", []) else ""
                col_desc = col.get("description", "")
                desc_text = f" -- {col_desc}" if col_desc else ""
                lines.append(f"    - {col['column_name']}: {col['data_type']} ({nullable}){pk_marker}{desc_text}")

            # Foreign keys
            if table_info.get("foreign_keys"):
                lines.append("  Foreign Keys:")
                for fk in table_info["foreign_keys"]:
                    lines.append(f"    - {fk['column_name']} -> {fk['foreign_table_name']}.{fk['foreign_column_name']}")

            lines.append("")

        # Add relationships summary
        if "relationships" in schema:
            lines.append("RELATIONSHIPS:")
            lines.append("-" * 40)
            for rel in schema["relationships"]:
                lines.append(f"  {rel}")
        
        # Add critical JOIN information
        lines.append("")
        lines.append("CRITICAL JOIN INFORMATION:")
        lines.append("-" * 40)
        lines.append("  detections.pipeline_id (varchar) joins to pipelines.pipeline_id (varchar)")
        lines.append("  CORRECT: JOIN pipelines p ON d.pipeline_id = p.pipeline_id")
        lines.append("  WRONG:   JOIN pipelines p ON d.pipeline_id = p.id (type mismatch)")
        lines.append("")

        return "\n".join(lines)

    def execute_query(self, sql: str) -> dict:
        """Execute a SQL query safely."""
        logger.debug(f"[DB] Executing query ({len(sql)} chars): {sql[:200]}...")
        
        # Layer 1: AST validation. Authoritative, and fails closed.
        #
        # The regex gate below was bypassable in two ways, both verified
        # against the live database before this layer existed:
        #   `BEGIN READ WRITE; DELETE FROM ...` — the connection's
        #   default_transaction_read_only is a session *default*, not a
        #   constraint, and an explicit BEGIN overrides it.
        #   `SELECT username, password_hash FROM users` — a pure SELECT passes
        #   every regex, and there was no table allowlist.
        verdict = validate_sql(sql, self.sql_policy)
        if not verdict.allowed:
            logger.warning(
                "[DB] Query rejected by AST guard: code=%s tables=%s",
                verdict.code, verdict.tables,
            )
            return {
                "success": False,
                "error": f"Security: {verdict.reason}",
                "error_code": verdict.code,
                "rows": [],
                "row_count": 0
            }

        # The guard returns SQL carrying an enforced LIMIT. Without one the
        # server still materialises the entire result set before fetchmany
        # stops reading it.
        sql = verdict.sql or sql

        # Layer 2: the original regex gate, retained as defence in depth. It is
        # no longer the only thing between generated text and the database.
        validation = self._validate_query(sql)
        if not validation["is_safe"]:
            logger.warning(f"[DB] Query validation failed: {validation['reason']}")
            return {
                "success": False,
                "error": validation["reason"],
                "rows": [],
                "row_count": 0
            }

        try:
            start_time = time.time()
            
            MAX_ROWS = self.MAX_ROWS  # cap result size: LLM answers never need more, and
                            # unbounded fetchall() can balloon memory/latency

            with self.get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql)
                    rows = cur.fetchmany(MAX_ROWS)
                    truncated = len(rows) == MAX_ROWS and cur.fetchone() is not None

                    execution_time = time.time() - start_time
                    row_count = len(rows)

                    if truncated:
                        logger.warning(f"[DB] Query result truncated to {MAX_ROWS} rows")
                    logger.info(f"[DB] Query executed successfully in {execution_time:.3f}s - {row_count} rows returned")

                    return {
                        "success": True,
                        "rows": [dict(row) for row in rows],
                        "row_count": row_count,
                        "truncated": truncated,
                        "columns": [desc[0] for desc in cur.description] if cur.description else []
                    }
        except Exception as e:
            logger.error(f"[DB] Query execution failed: {type(e).__name__}: {str(e)}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "rows": [],
                "row_count": 0
            }

    def _validate_query(self, sql: str) -> dict:
        """
        Validate that a query is safe (read-only).
        Only SELECT, WITH (CTE), and EXPLAIN queries are allowed.
        All DML (INSERT, UPDATE, DELETE) and DDL (CREATE, DROP, ALTER, etc.) operations are blocked.
        """
        if not sql or not sql.strip():
            return {
                "is_safe": False,
                "reason": "Empty query is not allowed"
            }
        
        # Normalize the SQL for checking
        sql_upper = sql.upper().strip()
        
        # Remove comments to avoid false positives
        # Remove single-line comments (--)
        sql_no_comments = re.sub(r'--.*', '', sql_upper)
        # Remove multi-line comments (/* */)
        sql_no_comments = re.sub(r'/\*.*?\*/', '', sql_no_comments, flags=re.DOTALL)

        # List of forbidden DML operations (Data Manipulation Language)
        forbidden_dml = [
            (r'\bDELETE\b', 'DELETE'),
            (r'\bUPDATE\b', 'UPDATE'),
            (r'\bINSERT\b', 'INSERT'),
            (r'\bMERGE\b', 'MERGE'),
            (r'\bUPSERT\b', 'UPSERT'),
            (r'\bREPLACE\b', 'REPLACE'),
        ]
        
        # List of forbidden DDL operations (Data Definition Language)
        forbidden_ddl = [
            (r'\bDROP\b', 'DROP'),
            (r'\bALTER\b', 'ALTER'),
            (r'\bTRUNCATE\b', 'TRUNCATE'),
            (r'\bCREATE\b', 'CREATE'),
            (r'\bRENAME\b', 'RENAME'),
        ]
        
        # List of forbidden security/permission operations
        forbidden_security = [
            (r'\bGRANT\b', 'GRANT'),
            (r'\bREVOKE\b', 'REVOKE'),
        ]
        
        # List of forbidden execution operations
        forbidden_exec = [
            (r'\bEXEC\b', 'EXEC'),
            (r'\bEXECUTE\b', 'EXECUTE'),
            (r'\bCALL\b', 'CALL'),
        ]
        
        # List of forbidden transaction operations
        forbidden_txn = [
            (r'\bCOMMIT\b', 'COMMIT'),
            (r'\bROLLBACK\b', 'ROLLBACK'),
            (r'\bBEGIN\b', 'BEGIN'),
            (r'\bSTART\s+TRANSACTION\b', 'START TRANSACTION'),
        ]
        
        # List of forbidden copy/backup operations
        forbidden_copy = [
            (r'\bCOPY\b', 'COPY'),
        ]
        
        # Combine all forbidden patterns
        all_forbidden = (
            forbidden_dml + forbidden_ddl + forbidden_security + 
            forbidden_exec + forbidden_txn + forbidden_copy
        )

        # Check for forbidden operations (case-insensitive, anywhere in query)
        # Use word boundaries to match SQL keywords, not identifiers
        for pattern, operation in all_forbidden:
            if re.search(pattern, sql_no_comments, re.IGNORECASE):
                return {
                    "is_safe": False,
                    "reason": f"Security: {operation} operations are not allowed. This SQL agent is read-only and can only execute SELECT queries."
                }
        
        # Additional strict check: Look for DELETE/UPDATE/INSERT as SQL statements
        # This catches attempts to use them even after CTEs or in complex queries
        # Check for DELETE/UPDATE/INSERT followed by FROM/SET/VALUES (SQL statement patterns)
        dangerous_statement_patterns = [
            (r'\bDELETE\s+FROM\b', 'DELETE statement'),
            (r'\bDELETE\s+.*\s+WHERE\b', 'DELETE statement with WHERE'),
            (r'\bUPDATE\s+\w+\s+SET\b', 'UPDATE statement'),
            (r'\bINSERT\s+INTO\b', 'INSERT statement'),
            (r'\bINSERT\s+.*\s+VALUES\b', 'INSERT statement with VALUES'),
        ]
        
        for pattern, attack_type in dangerous_statement_patterns:
            if re.search(pattern, sql_no_comments, re.IGNORECASE):
                return {
                    "is_safe": False,
                    "reason": f"Security: {attack_type} detected. This SQL agent is read-only and cannot execute data modification operations."
                }

        # Must start with SELECT, WITH (CTE), or EXPLAIN
        if not re.match(r'^\s*(SELECT|WITH|EXPLAIN)\b', sql_no_comments):
            return {
                "is_safe": False,
                "reason": "Security: Only SELECT, WITH (CTE), and EXPLAIN queries are allowed. This SQL agent is read-only."
            }
        
        # Additional check: Ensure no semicolon-separated multiple statements
        # This prevents SQL injection via statement chaining
        statements = [s.strip() for s in sql.split(';') if s.strip()]
        if len(statements) > 1:
            return {
                "is_safe": False,
                "reason": "Security: Multiple statements are not allowed. Only single SELECT queries are permitted."
            }
        
        # Check for common SQL injection patterns
        dangerous_patterns = [
            (r';\s*(DELETE|UPDATE|INSERT|DROP|ALTER)', 'Statement chaining'),
            (r'UNION\s+.*\s+(DELETE|UPDATE|INSERT|DROP|ALTER)', 'UNION-based injection'),
        ]
        
        for pattern, attack_type in dangerous_patterns:
            if re.search(pattern, sql_no_comments, re.IGNORECASE):
                return {
                    "is_safe": False,
                    "reason": f"Security: Potential {attack_type} detected. Only SELECT queries are allowed."
                }

        return {"is_safe": True, "reason": "Query is safe and read-only"}
