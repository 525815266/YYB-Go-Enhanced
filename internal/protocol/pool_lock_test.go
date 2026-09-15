package protocol

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestCodeLockSerializesSameAccount(t *testing.T) {
	p := NewPool(DefaultConfig(), nil)
	firstEntered := make(chan struct{})
	secondEntered := make(chan struct{})
	releaseFirst := make(chan struct{})
	releaseSecond := make(chan struct{})
	var wg sync.WaitGroup

	acquireForTest := func(entered chan<- struct{}, accountID int64, releaseSignal <-chan struct{}) {
		defer wg.Done()
		lock := p.codeLockFor(accountID)
		if err := acquire(context.Background(), lock); err != nil {
			t.Errorf("acquire code lock: %v", err)
			return
		}
		defer release(lock)
		close(entered)
		<-releaseSignal
	}

	wg.Add(2)
	go acquireForTest(firstEntered, 7, releaseFirst)
	<-firstEntered
	go acquireForTest(secondEntered, 7, releaseSecond)

	select {
	case <-secondEntered:
		t.Fatal("same account acquired two code locks concurrently")
	case <-time.After(30 * time.Millisecond):
	}
	close(releaseFirst)
	select {
	case <-secondEntered:
	case <-time.After(time.Second):
		t.Fatal("second same-account request did not acquire after the first released")
	}
	close(releaseSecond)
	wg.Wait()
}

func TestCodeLocksAllowDifferentAccountsConcurrently(t *testing.T) {
	p := NewPool(DefaultConfig(), nil)
	started := make(chan struct{}, 2)
	release := make(chan struct{})
	var active int32
	var maxActive int32
	var wg sync.WaitGroup

	for _, accountID := range []int64{1, 2} {
		wg.Add(1)
		go func(accountID int64) {
			defer wg.Done()
			lock := p.codeLockFor(accountID)
			if err := acquire(context.Background(), lock); err != nil {
				t.Errorf("acquire code lock: %v", err)
				return
			}
			defer release(lock)
			current := atomic.AddInt32(&active, 1)
			for {
				old := atomic.LoadInt32(&maxActive)
				if current <= old || atomic.CompareAndSwapInt32(&maxActive, old, current) {
					break
				}
			}
			started <- struct{}{}
			<-release
			atomic.AddInt32(&active, -1)
		}(accountID)
	}

	for range 2 {
		select {
		case <-started:
		case <-time.After(time.Second):
			t.Fatal("different-account code locks did not run concurrently")
		}
	}
	if got := atomic.LoadInt32(&maxActive); got != 2 {
		t.Fatalf("max concurrent code locks = %d, want 2", got)
	}
	close(release)
	wg.Wait()
}
