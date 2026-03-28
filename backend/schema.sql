-- ═══════════════════════════════════════════════════════════
-- SHIVA'S Digital Evaluation — Schema v3.1
-- SAFE TO RUN MULTIPLE TIMES on existing Supabase databases
-- ═══════════════════════════════════════════════════════════

-- STEP 1: Create tables if they don't exist yet
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(100) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL DEFAULT '',
    full_name     VARCHAR(200),
    role          VARCHAR(20) NOT NULL DEFAULT 'student',
    created_at    TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS students (
    id          SERIAL PRIMARY KEY,
    student_id  VARCHAR(50) UNIQUE NOT NULL,
    full_name   VARCHAR(200) NOT NULL DEFAULT '',
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS exam_sessions (
    id              SERIAL PRIMARY KEY,
    student_id      VARCHAR(50),
    script_filename TEXT,
    uploaded_at     TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS evaluations (
    id              SERIAL PRIMARY KEY,
    student_id      VARCHAR(50) NOT NULL,
    evaluator_type  VARCHAR(20) NOT NULL,
    evaluator_name  VARCHAR(200),
    marks           JSONB NOT NULL DEFAULT '{}',
    justification   TEXT,
    ai_feedback     JSONB,
    ai_confidence   FLOAT,
    ai_htr_text     TEXT,
    submitted_at    TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS final_decisions (
    id                  SERIAL PRIMARY KEY,
    student_id          VARCHAR(50) NOT NULL,
    final_marks         JSONB NOT NULL DEFAULT '{}',
    selected_evaluator  VARCHAR(20),
    deviation_percent   FLOAT,
    moderator           VARCHAR(200),
    notes               TEXT,
    finalized_at        TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           SERIAL PRIMARY KEY,
    action       VARCHAR(100),
    student_id   VARCHAR(50),
    performed_by VARCHAR(200),
    details      JSONB,
    created_at   TIMESTAMP DEFAULT NOW()
);

-- STEP 2: Add missing columns safely (no error if they already exist)
ALTER TABLE users     ADD COLUMN IF NOT EXISTS status   VARCHAR(20) DEFAULT 'approved';
ALTER TABLE users     ADD COLUMN IF NOT EXISTS email    VARCHAR(200);
ALTER TABLE students  ADD COLUMN IF NOT EXISTS status   VARCHAR(20) DEFAULT 'pending';
ALTER TABLE students  ADD COLUMN IF NOT EXISTS username VARCHAR(100);

-- STEP 3: Add unique constraints safely
DO $$ BEGIN
  ALTER TABLE evaluations ADD CONSTRAINT uq_eval_stu_type UNIQUE (student_id, evaluator_type);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE final_decisions ADD CONSTRAINT uq_final_stu UNIQUE (student_id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE exam_sessions ADD CONSTRAINT uq_session_stu UNIQUE (student_id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL;
END $$;

-- STEP 4: Indexes
CREATE INDEX IF NOT EXISTS idx_evals_student     ON evaluations(student_id);
CREATE INDEX IF NOT EXISTS idx_evals_type        ON evaluations(evaluator_type);
CREATE INDEX IF NOT EXISTS idx_finals_student    ON final_decisions(student_id);
CREATE INDEX IF NOT EXISTS idx_students_username ON students(username);

-- STEP 5: Seed default users
-- All demo users use password "pass123" (SHA-256 hash below)
-- hash of "pass123" = ef92b778bafe771e89245b89ecbc08a44a4e166c06659911881f383d4473e94f
INSERT INTO users (username, password_hash, full_name, role, status) VALUES
  ('admin_faculty', 'ef92b778bafe771e89245b89ecbc08a44a4e166c06659911881f383d4473e94f', 'Dr. Priya Sharma',   'faculty', 'approved'),
  ('hod_user',      'ef92b778bafe771e89245b89ecbc08a44a4e166c06659911881f383d4473e94f', 'Prof. Ramesh Kumar', 'hod',     'approved'),
  ('student_001',   'ef92b778bafe771e89245b89ecbc08a44a4e166c06659911881f383d4473e94f', 'Arjun Mehta',        'student', 'approved'),
  ('student_002',   'ef92b778bafe771e89245b89ecbc08a44a4e166c06659911881f383d4473e94f', 'Priya Nair',         'student', 'approved'),
  ('student_003',   'ef92b778bafe771e89245b89ecbc08a44a4e166c06659911881f383d4473e94f', 'Rahul Singh',        'student', 'approved'),
  ('student_004',   'ef92b778bafe771e89245b89ecbc08a44a4e166c06659911881f383d4473e94f', 'Ananya Reddy',       'student', 'approved')
ON CONFLICT (username) DO UPDATE SET
  status    = EXCLUDED.status,
  full_name = EXCLUDED.full_name;

INSERT INTO students (student_id, full_name, username, status) VALUES
  ('STU-8829', 'Arjun Mehta',  'student_001', 'pending'),
  ('STU-8830', 'Priya Nair',   'student_002', 'graded'),
  ('STU-8831', 'Rahul Singh',  'student_003', 'flagged'),
  ('STU-8832', 'Ananya Reddy', 'student_004', 'pending')
ON CONFLICT (student_id) DO UPDATE SET
  full_name = EXCLUDED.full_name,
  username  = EXCLUDED.username;

-- STEP 6: Verify (run these separately to check)
-- SELECT * FROM users;
-- SELECT * FROM students;
