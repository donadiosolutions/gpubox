package runner

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"

	"github.com/donadiosolutions/gpubox/sbom-generator/internal/compact"
)

// Config controls one BuildKit scanner-protocol run.
type Config struct {
	ScannerPath string
	Destination string
	MaxBytes    int64
}

// Run executes the scanner, discovers its top-level SPDX outputs, validates
// and compacts every document in memory, and commits them together.
func Run(ctx context.Context, cfg Config) error {
	if ctx == nil {
		return fmt.Errorf("run scanner: context is nil")
	}
	if cfg.ScannerPath == "" {
		return fmt.Errorf("run scanner: scanner path is empty")
	}
	if cfg.Destination == "" {
		return fmt.Errorf("validate scanner destination: destination is empty")
	}
	if cfg.MaxBytes <= 0 {
		return fmt.Errorf("validate scanner output size: maximum size must be positive")
	}
	destinationInfo, err := os.Stat(cfg.Destination)
	if err != nil {
		return fmt.Errorf("stat scanner destination %q: %w", cfg.Destination, err)
	}
	if !destinationInfo.IsDir() {
		return fmt.Errorf("validate scanner destination %q: path is not a directory", cfg.Destination)
	}

	cmd := exec.CommandContext(ctx, cfg.ScannerPath)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = os.Environ()
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("run scanner %q: %w", cfg.ScannerPath, err)
	}

	paths, err := discoverOutputs(cfg.Destination)
	if err != nil {
		return err
	}
	if len(paths) == 0 {
		return fmt.Errorf("discover scanner outputs in %q: no .spdx.json files found", cfg.Destination)
	}

	prepared := make([]compact.Prepared, 0, len(paths))
	for _, path := range paths {
		item, err := compact.PrepareFile(path, cfg.MaxBytes)
		if err != nil {
			return fmt.Errorf("prepare scanner output %q: %w", path, err)
		}
		prepared = append(prepared, item)
	}
	if err := compact.CommitFiles(prepared); err != nil {
		return fmt.Errorf("commit scanner outputs: %w", err)
	}
	return nil
}

func discoverOutputs(destination string) ([]string, error) {
	entries, err := os.ReadDir(destination)
	if err != nil {
		return nil, fmt.Errorf("discover scanner outputs in %q: %w", destination, err)
	}
	paths := make([]string, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".spdx.json") {
			continue
		}
		paths = append(paths, filepath.Join(destination, entry.Name()))
	}
	sort.Strings(paths)
	return paths, nil
}
