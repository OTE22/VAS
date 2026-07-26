# Commands to Clear Database Data in `face_recognition_db` Container

## Database Configuration
- **Container Name**: `face_recognition_db`
- **Database Name**: `face_recognition`
- **User**: `postgres`
- **Password**: `admin`

---

## Option 1: Drop All Tables (Keeps Database, Removes All Data & Schema)

This will remove all tables and their data, but keep the database itself:

```bash
# Connect to the container and drop all tables
docker exec -it face_recognition_db psql -U postgres -d face_recognition -c "
DO \$\$ 
DECLARE 
    r RECORD;
BEGIN
    FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') 
    LOOP
        EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE';
    END LOOP;
END \$\$;
"
```

---

## Option 2: Truncate All Tables (Keeps Schema, Removes Data Only)

This keeps all tables and structure but removes all data:

```bash
# Connect to the container and truncate all tables
docker exec -it face_recognition_db psql -U postgres -d face_recognition -c "
DO \$\$ 
DECLARE 
    r RECORD;
BEGIN
    FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') 
    LOOP
        EXECUTE 'TRUNCATE TABLE ' || quote_ident(r.tablename) || ' CASCADE';
    END LOOP;
END \$\$;
"
```

---

## Option 3: Drop and Recreate Database (Complete Reset)

This completely removes and recreates the database:

```bash
# Stop the container first (optional, but safer)
docker stop face_recognition_db

# Drop and recreate the database
docker exec -it face_recognition_db psql -U postgres -c "
DROP DATABASE IF EXISTS face_recognition;
CREATE DATABASE face_recognition;
"

# Restart the container
docker start face_recognition_db
```

---

## Option 4: Remove Docker Volume (Complete Reset Including Schema)

This removes the entire PostgreSQL data volume (most thorough reset):

```bash
# Stop the container
docker stop face_recognition_db

# Remove the container (optional, if you want to recreate it)
docker rm face_recognition_db

# Remove the volume (this deletes ALL data)
docker volume rm postgres_data

# Restart with docker-compose (this will recreate the volume and initialize the database)
docker-compose up -d postgres
```

**Note**: If using docker-compose from a specific directory:
```bash
# From the directory containing docker-compose.yml
docker-compose down postgres
docker volume rm face_detector_postgres_data  # or check actual volume name with: docker volume ls
docker-compose up -d postgres
```

---

## Option 5: Interactive psql Session (Manual Control)

Connect interactively to the database:

```bash
# Connect to PostgreSQL inside the container
docker exec -it face_recognition_db psql -U postgres -d face_recognition

# Then run SQL commands:
# \dt                    # List all tables
# TRUNCATE TABLE faces CASCADE;  # Clear specific table
# DROP TABLE faces CASCADE;      # Drop specific table
# \q                    # Exit
```

---

## Quick Commands Reference

### Check what tables exist:
```bash
docker exec -it face_recognition_db psql -U postgres -d face_recognition -c "\dt"
```

### Check database size:
```bash
docker exec -it face_recognition_db psql -U postgres -d face_recognition -c "
SELECT pg_size_pretty(pg_database_size('face_recognition'));
"
```

### List all databases:
```bash
docker exec -it face_recognition_db psql -U postgres -c "\l"
```

### Check volume name:
```bash
docker volume ls | grep postgres
```

---

## Recommended Approach

For a **quick data reset** (keeping schema): Use **Option 2** (Truncate)
For a **complete reset** (including schema): Use **Option 3** (Drop & Recreate)
For a **nuclear reset** (including volume): Use **Option 4** (Remove Volume)

