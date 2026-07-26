"""
SQL Tools Module
================
SQL validation and fixing tools using LangGraph Tool Calling Pattern.
"""

import re
import json
from typing import Any, Dict

from langchain_core.tools import tool


@tool
def prepare_sql_from_llm_response(llm_response: str) -> Dict[str, Any]:
    """
    Parses LLM JSON response and prepares SQL for execution.

    Takes the raw LLM response containing SQL (possibly with Python-style
    string concatenation, escape characters, etc.) and produces a clean,
    executable SQL query.

    Args:
        llm_response: Raw LLM response string containing JSON with sql and purpose fields

    Returns:
        Dictionary containing:
        - success: bool - Whether parsing was successful
        - sql: str - Clean, executable SQL query
        - purpose: str - Description of what the query does
        - transformations: List[str] - List of transformations applied
        - error: str - Error message if parsing failed
    """
    transformations = []

    try:
        # Step 1: Try to extract JSON from the response
        # Handle markdown code blocks
        cleaned_response = llm_response.strip()
        if '```json' in cleaned_response:
            json_match = re.search(r'```json\s*([\s\S]*?)\s*```', cleaned_response)
            if json_match:
                cleaned_response = json_match.group(1)
                transformations.append("Extracted JSON from markdown code block")
        elif '```' in cleaned_response:
            json_match = re.search(r'```\s*([\s\S]*?)\s*```', cleaned_response)
            if json_match:
                cleaned_response = json_match.group(1)
                transformations.append("Extracted content from code block")

        # Step 2: Handle Python-style string concatenation in JSON
        # Pattern: "string1"\n         + "string2" -> "string1 string2"
        # This is the common LLM error where it outputs:
        # "sql": "SELECT ...\n"
        #        + "FROM ...\n"
        # First, handle with + operator (most common case)
        # Match: "..."\n          + "..." and replace with "..." "..." (space between)
        python_concat_with_plus = r'"([^"]*(?:\\.[^"]*)*)"\s*\n\s*\+\s*"'
        if re.search(python_concat_with_plus, cleaned_response):
            transformations.append("Fixed Python-style string concatenation with + operator")
            while re.search(python_concat_with_plus, cleaned_response):
                # Replace pattern: "text1"\n + "text2" -> "text1 text2"
                cleaned_response = re.sub(python_concat_with_plus, r'"\1 ', cleaned_response)
        
        # Pattern: "string1"\n         "string2" -> "string1 string2" (without +)
        python_concat_pattern = r'"([^"]*(?:\\.[^"]*)*)"\s*\n\s*"'
        if re.search(python_concat_pattern, cleaned_response):
            transformations.append("Fixed Python-style string concatenation")
            while re.search(python_concat_pattern, cleaned_response):
                cleaned_response = re.sub(python_concat_pattern, r'"\1 ', cleaned_response)

        # Also handle: "string1" "string2" (adjacent strings)
        adjacent_strings_pattern = r'"([^"]*(?:\\.[^"]*)*)"\s+"'
        if re.search(adjacent_strings_pattern, cleaned_response):
            transformations.append("Merged adjacent string literals")
            while re.search(adjacent_strings_pattern, cleaned_response):
                cleaned_response = re.sub(adjacent_strings_pattern, r'"\1 ', cleaned_response)

        # Step 3: Try to parse as JSON
        sql = ""
        purpose = ""
        parsed_json = False

        try:
            # Find JSON object in the response
            json_obj_match = re.search(r'\{[\s\S]*\}', cleaned_response)
            if json_obj_match:
                json_str = json_obj_match.group()

                # Clean up the JSON string for parsing
                # Handle escaped newlines within strings
                json_str = json_str.replace('\\\n', ' ')

                data = json.loads(json_str)
                sql = data.get("sql", "")
                purpose = data.get("purpose", "")
                parsed_json = True
                transformations.append("Parsed JSON successfully")
        except json.JSONDecodeError as e:
            # JSON parsing failed, try regex extraction
            transformations.append(f"JSON parsing failed: {str(e)}")

        # Step 4: Fallback - Extract SQL using regex patterns
        if not sql:
            # Try standard patterns first (after cleaning, JSON should parse)
            sql_patterns = [
                r'"sql"\s*:\s*"((?:[^"\\]|\\.)*)\"',  # Standard JSON string
                r'"sql"\s*:\s*`((?:[^`])*)`',          # Backtick string
                r'"sql"\s*:\s*\'((?:[^\'\\]|\\.)*)\'', # Single quote string
            ]

            for pattern in sql_patterns:
                match = re.search(pattern, cleaned_response, re.DOTALL)
                if match:
                    sql = match.group(1)
                    transformations.append("Extracted SQL using regex pattern")
                    break

            # If still no SQL, try extracting from original response
            if not sql:
                for pattern in sql_patterns:
                    match = re.search(pattern, llm_response, re.DOTALL)
                    if match:
                        sql = match.group(1)
                        transformations.append("Extracted SQL using regex pattern from original")
                        break

            # Try to extract purpose too
            purpose_match = re.search(r'"purpose"\s*:\s*"((?:[^"\\]|\\.)*)"', cleaned_response)
            if not purpose_match:
                purpose_match = re.search(r'"purpose"\s*:\s*"((?:[^"\\]|\\.)*)"', llm_response)
            if purpose_match:
                purpose = purpose_match.group(1)

        # Step 5: If still no SQL, try to find raw SELECT statement
        if not sql:
            select_match = re.search(
                r'SELECT\s+[\s\S]+?(?:LIMIT\s+\d+|DESC\s*;?|ASC\s*;?|;|\Z)',
                llm_response,
                re.IGNORECASE
            )
            if select_match:
                sql = select_match.group().strip()
                transformations.append("Extracted raw SELECT statement")

        if not sql:
            return {
                "success": False,
                "sql": "",
                "purpose": "",
                "transformations": transformations,
                "error": "Could not extract SQL from LLM response"
            }

        # Step 6: Clean up the extracted SQL
        original_sql = sql

        # Remove escape characters
        sql = sql.replace('\\n', '\n')
        sql = sql.replace('\\t', '\t')
        sql = sql.replace('\\"', '"')
        sql = sql.replace("\\'", "'")
        sql = sql.replace('\\\\', '\\')
        if sql != original_sql:
            transformations.append("Unescaped special characters")

        # Remove any remaining markdown code block markers
        sql = re.sub(r'```\w*\s*', '', sql)
        sql = sql.replace('```', '')

        # Remove trailing semicolons (psycopg2 handles statement termination)
        sql = sql.rstrip(';').strip()
        if original_sql.rstrip() != sql:
            transformations.append("Removed trailing semicolon")

        # Normalize excessive whitespace but preserve structure
        sql = re.sub(r'\n\s*\n+', '\n', sql)  # Multiple blank lines -> single newline
        sql = re.sub(r'[ \t]+', ' ', sql)     # Multiple spaces/tabs -> single space

        # Clean up line breaks around SQL keywords for readability
        sql_keywords = [
            'SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'JOIN', 'LEFT JOIN',
            'RIGHT JOIN', 'INNER JOIN', 'OUTER JOIN', 'FULL JOIN', 'CROSS JOIN',
            'GROUP BY', 'ORDER BY', 'HAVING', 'LIMIT', 'OFFSET', 'UNION',
            'INTERSECT', 'EXCEPT', 'WITH', 'AS'
        ]

        for kw in sql_keywords:
            # Add newline before major keywords (except at start)
            sql = re.sub(rf'(?<!^)\s+({kw})\b', rf'\n\1', sql, flags=re.IGNORECASE)

        # Final cleanup
        sql = sql.strip()
        sql = re.sub(r'\n ', '\n', sql)  # Remove leading spaces after newlines

        if sql:
            transformations.append("Normalized SQL formatting")

        # Clean up purpose too
        purpose = purpose.replace('\\n', ' ').replace('\\t', ' ').strip()

        return {
            "success": True,
            "sql": sql,
            "purpose": purpose,
            "transformations": transformations,
            "error": ""
        }

    except Exception as e:
        return {
            "success": False,
            "sql": "",
            "purpose": "",
            "transformations": transformations,
            "error": f"Unexpected error: {str(e)}"
        }


@tool
def validate_sql_query(sql_query: str) -> Dict[str, Any]:
    """
    Validates a SQL query for syntax errors and common issues.

    Args:
        sql_query: The SQL query string to validate

    Returns:
        Dictionary containing:
        - is_valid: bool - Whether the query is valid
        - errors: List[str] - List of validation errors found
        - warnings: List[str] - List of warnings (non-critical issues)
    """
    errors = []
    warnings = []
    sql_upper = sql_query.upper().strip()

    # Check 1: Must start with SELECT, WITH, or EXPLAIN
    if not re.match(r'^\s*(SELECT|WITH|EXPLAIN)\b', sql_upper):
        errors.append("Query must start with SELECT, WITH, or EXPLAIN")

    # Check 2: Balanced parentheses
    open_parens = sql_query.count('(')
    close_parens = sql_query.count(')')
    if open_parens != close_parens:
        errors.append(f"Unbalanced parentheses: {open_parens} opening, {close_parens} closing")

    # Check 3: Balanced single quotes
    quote_count = len(re.findall(r"(?<!\\)'", sql_query))
    if quote_count % 2 != 0:
        errors.append("Unbalanced single quotes in string literals")

    # Check 4: Incomplete JOIN clause
    if re.search(r'\bJOIN\b', sql_upper):
        if not re.search(r'\bON\b', sql_upper) and not re.search(r'\bUSING\b', sql_upper):
            errors.append("JOIN clause missing ON or USING condition")

    # Check 5: Python string concatenation (common LLM error)
    if re.search(r'"\s*"', sql_query) or re.search(r"'\s*\+\s*'", sql_query):
        errors.append("Contains Python-style string concatenation")

    # Check 6: Incomplete WHERE clause
    if re.search(r'WHERE\s*$', sql_upper):
        errors.append("WHERE clause is empty")

    # Check 7: Missing FROM in SELECT with columns
    if re.search(r'\bSELECT\b', sql_upper) and not re.search(r'\bFROM\b', sql_upper):
        # Allow SELECT without FROM for simple expressions like SELECT 1, SELECT NOW()
        if not re.search(r'SELECT\s+[\d\'\"\w\(\)]+\s*[,;]?\s*$', sql_upper):
            warnings.append("SELECT query without FROM clause - verify this is intentional")

    # Check 8: Check for double semicolons
    if re.search(r';\s*;', sql_query):
        errors.append("Contains double semicolons")

    # Check 9: Common JOIN error - detections.pipeline_id should join to pipelines.pipeline_id, not pipelines.id
    if re.search(r'\bJOIN\s+pipelines\s+\w+\s+ON\s+.*\.pipeline_id\s*=\s*\w+\.id\b', sql_query, re.IGNORECASE):
        if re.search(r'd\.pipeline_id\s*=\s*p\.id\b', sql_query, re.IGNORECASE):
            errors.append("Incorrect JOIN: detections.pipeline_id should join to pipelines.pipeline_id (varchar), not pipelines.id (integer). Use: d.pipeline_id = p.pipeline_id")

    # Return validation result
    return {
        "is_valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings
    }
