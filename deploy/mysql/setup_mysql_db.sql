-- MySQL Database Setup for Canon Wars
-- Run this script as a MySQL root user to set up the database and user

-- Create database
CREATE DATABASE IF NOT EXISTS canonwars 
  CHARACTER SET utf8mb4 
  COLLATE utf8mb4_unicode_ci;

-- Create user (change password in production!)
CREATE USER IF NOT EXISTS 'canonwars_user'@'localhost' 
  IDENTIFIED BY 'secure_password_change_me';

-- Grant privileges
GRANT ALL PRIVILEGES ON canonwars.* TO 'canonwars_user'@'localhost';

-- For remote connections (optional, adjust host as needed)
-- CREATE USER IF NOT EXISTS 'canonwars_user'@'%' 
--   IDENTIFIED BY 'secure_password_change_me';
-- GRANT ALL PRIVILEGES ON canonwars.* TO 'canonwars_user'@'%';

-- Flush privileges to apply changes
FLUSH PRIVILEGES;

-- Show created database and user
SHOW DATABASES LIKE 'canonwars';
SELECT User, Host FROM mysql.user WHERE User = 'canonwars_user';

-- Display success message
SELECT 'Database setup complete! Update your .env file with these credentials.' AS message;
