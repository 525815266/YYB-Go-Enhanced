package store

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"testing"
	"time"
)

// database/sql can return from Close while a previously started Connect is
// still creating a database. Files must stop changing before DB.Close returns.
func TestCloseWaitsForPendingConnection(t *testing.T) {
	connector := &closingTestConnector{started: make(chan struct{}), release: make(chan struct{}),
		stopping: make(chan struct{}), connectionClosed: make(chan struct{})}
	raw := sql.OpenDB(connector)
	db := &DB{sql: raw}
	pingDone := make(chan error, 1)
	go func() { pingDone <- raw.PingContext(context.Background()) }()
	<-connector.started
	done := make(chan error, 1)
	go func() { done <- db.Close() }()
	<-connector.stopping
	select {
	case err := <-done:
		close(connector.release)
		<-pingDone
		t.Fatalf("Close returned before pending connection stopped: %v", err)
	case <-time.After(20 * time.Millisecond):
	}
	close(connector.release)
	<-pingDone
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Close: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("Close did not complete after pending connection stopped")
	}
	select {
	case <-connector.connectionClosed:
	default:
		t.Fatal("underlying connection was not closed")
	}
}

type closingTestConnector struct {
	started, release, stopping, connectionClosed chan struct{}
}

func (c *closingTestConnector) Connect(context.Context) (driver.Conn, error) {
	close(c.started)
	<-c.release
	return &closingTestConn{closed: c.connectionClosed}, nil
}
func (c *closingTestConnector) Driver() driver.Driver { return closingTestDriver{} }
func (c *closingTestConnector) Close() error          { close(c.stopping); return nil }

type closingTestDriver struct{}

func (closingTestDriver) Open(string) (driver.Conn, error) { return nil, errors.New("use connector") }

type closingTestConn struct{ closed chan struct{} }

func (c *closingTestConn) Close() error                      { close(c.closed); return nil }
func (*closingTestConn) Prepare(string) (driver.Stmt, error) { return nil, errors.New("unused") }
func (*closingTestConn) Begin() (driver.Tx, error)           { return nil, errors.New("unused") }
