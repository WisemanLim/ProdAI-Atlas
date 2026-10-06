// Package store 는 Manager 상태 저장소다. local=SQLite(순수 Go 드라이버) · prod=PostgreSQL, 스키마는 goose 마이그레이션.
package store

import (
	"embed"
	"fmt"
	"strings"

	"github.com/glebarez/sqlite"
	"github.com/pressly/goose/v3"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

//go:embed migrations/*.sql
var migrations embed.FS

// Open 은 DB_ENGINE·DATABASE_URL 로 연결한다.
func Open(engine, url string) (*gorm.DB, error) {
	cfg := &gorm.Config{Logger: logger.Default.LogMode(logger.Warn), TranslateError: true}
	switch engine {
	case "sqlite":
		dsn := url
		if !strings.Contains(dsn, "?") {
			dsn += "?_pragma=foreign_keys(1)&_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)"
		}
		db, err := gorm.Open(sqlite.Open(dsn), cfg)
		if err != nil {
			return nil, err
		}
		sqlDB, _ := db.DB()
		sqlDB.SetMaxOpenConns(1) // SQLite 단일 writer
		return db, nil
	case "postgres":
		return gorm.Open(postgres.Open(url), cfg)
	default:
		return nil, fmt.Errorf("지원하지 않는 DB_ENGINE %q (sqlite | postgres)", engine)
	}
}

// migrateEngine 은 Go 마이그레이션이 방언을 고르는 데 쓴다.
var migrateEngine = "sqlite"

// Migrate 는 내장 마이그레이션을 최신으로 적용한다.
func Migrate(db *gorm.DB, engine string) error {
	migrateEngine = engine
	sqlDB, err := db.DB()
	if err != nil {
		return err
	}
	goose.SetBaseFS(migrations)
	goose.SetLogger(goose.NopLogger())
	dialect := map[string]string{"sqlite": "sqlite3", "postgres": "postgres"}[engine]
	if err := goose.SetDialect(dialect); err != nil {
		return err
	}
	return goose.Up(sqlDB, "migrations")
}
