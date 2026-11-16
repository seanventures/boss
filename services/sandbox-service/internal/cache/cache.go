// Package cache is boss-hostd's local inventory: a bbolt file that is a
// rebuildable cache, never a source of truth (LLD §2.5). Deleting it is always
// safe: on start the host rebuilds it from one desired-state resync plus a
// /proc and netlink scan.
//
// Nothing secret is ever written here: handles are stripped from the cached
// desired state, and keys live in memory or in the 0600 pki files.
package cache

import (
	"encoding/json"
	"os"
	"path/filepath"
	"time"

	bolt "go.etcd.io/bbolt"

	"github.com/seanventures/boss/services/sandbox-service/internal/hostapi"
)

var (
	bucketVMs  = []byte("vms")
	bucketMeta = []byte("meta")
	keyDesired = []byte("last_desired")
)

// VMRecord is what the host remembers about one VM it started.
type VMRecord struct {
	VMID         string    `json:"vm_id"`
	DeploymentID string    `json:"deployment_id"`
	Mode         string    `json:"vm_mode"`
	PID          int       `json:"pid"`
	Dir          string    `json:"dir"`
	Tap          string    `json:"tap,omitempty"`
	VMIP         string    `json:"vm_ip,omitempty"`
	ImageDigest  string    `json:"image_digest,omitempty"`
	StartedAt    time.Time `json:"started_at"`
	Restarts     []int64   `json:"restarts,omitempty"` // unix seconds of recent restarts
}

type Cache struct{ db *bolt.DB }

// Open opens (or creates) the cache file with 0600 permissions.
func Open(path string) (*Cache, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	db, err := bolt.Open(path, 0o600, &bolt.Options{Timeout: 2 * time.Second})
	if err != nil {
		return nil, err
	}
	err = db.Update(func(tx *bolt.Tx) error {
		if _, err := tx.CreateBucketIfNotExists(bucketVMs); err != nil {
			return err
		}
		_, err := tx.CreateBucketIfNotExists(bucketMeta)
		return err
	})
	if err != nil {
		db.Close()
		return nil, err
	}
	return &Cache{db: db}, nil
}

func (c *Cache) Close() error { return c.db.Close() }

func (c *Cache) PutVM(r VMRecord) error {
	raw, err := json.Marshal(r)
	if err != nil {
		return err
	}
	return c.db.Update(func(tx *bolt.Tx) error {
		return tx.Bucket(bucketVMs).Put([]byte(r.VMID), raw)
	})
}

func (c *Cache) DeleteVM(vmID string) error {
	return c.db.Update(func(tx *bolt.Tx) error {
		return tx.Bucket(bucketVMs).Delete([]byte(vmID))
	})
}

func (c *Cache) VMs() (map[string]VMRecord, error) {
	out := map[string]VMRecord{}
	err := c.db.View(func(tx *bolt.Tx) error {
		return tx.Bucket(bucketVMs).ForEach(func(k, v []byte) error {
			var r VMRecord
			if json.Unmarshal(v, &r) == nil {
				out[string(k)] = r
			}
			return nil
		})
	})
	return out, err
}

// PutDesired caches the last desired state with every handle removed.
func (c *Cache) PutDesired(ds hostapi.DesiredState) error {
	raw, err := json.Marshal(StripSecrets(ds))
	if err != nil {
		return err
	}
	return c.db.Update(func(tx *bolt.Tx) error {
		return tx.Bucket(bucketMeta).Put(keyDesired, raw)
	})
}

// LastDesired returns the cached desired state (handles stripped), if any.
func (c *Cache) LastDesired() (*hostapi.DesiredState, error) {
	var out *hostapi.DesiredState
	err := c.db.View(func(tx *bolt.Tx) error {
		raw := tx.Bucket(bucketMeta).Get(keyDesired)
		if raw == nil {
			return nil
		}
		var ds hostapi.DesiredState
		if err := json.Unmarshal(raw, &ds); err != nil {
			return nil // a corrupt cache entry is just a miss
		}
		out = &ds
		return nil
	})
	return out, err
}

// StripSecrets returns a deep-enough copy of ds with all handles blanked.
func StripSecrets(ds hostapi.DesiredState) hostapi.DesiredState {
	vms := make([]hostapi.DesiredVM, len(ds.VMs))
	for i, vm := range ds.VMs {
		vm.ReplicaHandle = ""
		runs := make([]hostapi.RunBind, len(vm.Runs))
		for j, rb := range vm.Runs {
			rb.Handles = hostapi.Handles{}
			runs[j] = rb
		}
		vm.Runs = runs
		vms[i] = vm
	}
	ds.VMs = vms
	return ds
}
