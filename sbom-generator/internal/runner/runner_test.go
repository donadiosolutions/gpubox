package runner

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/donadiosolutions/gpubox/sbom-generator/internal/compact"
)

const runnerStatement = `{
  "_type": "https://in-toto.io/Statement/v1",
  "predicateType": "https://spdx.dev/Document",
  "subject": [{"name":"runner-fixture","digest":{"sha256":"abc"}}],
  "predicate": {
    "SPDXID": "SPDXRef-DOCUMENT",
    "files": [{"SPDXID":"SPDXRef-File-Known","fileName":"/bin/known"}],
    "packages": [{"SPDXID":"SPDXRef-Package","name":"runner-package","versionInfo":"1.0.0","hasFiles":["SPDXRef-File-Known","SPDXRef-Unknown"]}],
    "relationships": [
      {"spdxElementId":"SPDXRef-DOCUMENT","relationshipType":"DESCRIBES","relatedSpdxElement":"SPDXRef-Package"},
      {"spdxElementId":"SPDXRef-Package","relationshipType":"CONTAINS","relatedSpdxElement":"SPDXRef-File-Known"}
    ]
  }
}`

func TestRunPropagatesScannerFailure(t *testing.T) {
	requireUnix(t)
	destination := t.TempDir()
	withScannerDestination(t, destination)
	scanner := writeScanner(t, "exit 23")

	err := Run(context.Background(), Config{
		ScannerPath: scanner,
		Destination: destination,
		MaxBytes:    compact.DefaultMaxBytes,
	})
	if err == nil {
		t.Fatal("Run() error = nil, want scanner failure")
	}
	if !strings.Contains(err.Error(), "scanner") {
		t.Fatalf("Run() error = %v, want scanner phase", err)
	}
}

func TestRunRejectsMissingDestination(t *testing.T) {
	requireUnix(t)
	scanner := writeScanner(t, "exit 0")

	err := Run(context.Background(), Config{
		ScannerPath: scanner,
		MaxBytes:    compact.DefaultMaxBytes,
	})
	if err == nil {
		t.Fatal("Run() error = nil, want missing-destination error")
	}
	if !strings.Contains(err.Error(), "destination") {
		t.Fatalf("Run() error = %v, want destination phase", err)
	}
}

func TestRunRejectsNonDirectoryDestination(t *testing.T) {
	requireUnix(t)
	destination := filepath.Join(t.TempDir(), "destination")
	if err := os.WriteFile(destination, []byte("not a directory"), 0o600); err != nil {
		t.Fatalf("write destination fixture: %v", err)
	}
	withScannerDestination(t, destination)
	scanner := writeScanner(t, "exit 0")

	err := Run(context.Background(), Config{
		ScannerPath: scanner,
		Destination: destination,
		MaxBytes:    compact.DefaultMaxBytes,
	})
	if err == nil {
		t.Fatal("Run() error = nil, want non-directory error")
	}
	if !strings.Contains(err.Error(), "destination") {
		t.Fatalf("Run() error = %v, want destination phase", err)
	}
}

func TestRunRejectsZeroOutputs(t *testing.T) {
	requireUnix(t)
	destination := t.TempDir()
	withScannerDestination(t, destination)
	scanner := writeScanner(t, "touch \"$BUILDKIT_SCAN_DESTINATION/not-an-sbom.txt\"\nexit 0")

	err := Run(context.Background(), Config{
		ScannerPath: scanner,
		Destination: destination,
		MaxBytes:    compact.DefaultMaxBytes,
	})
	if err == nil {
		t.Fatal("Run() error = nil, want zero-output error")
	}
	if !strings.Contains(err.Error(), "output") {
		t.Fatalf("Run() error = %v, want output-discovery phase", err)
	}
}

func TestRunCompactsEverySPDXOutput(t *testing.T) {
	requireUnix(t)
	destination := t.TempDir()
	withScannerDestination(t, destination)
	scanner := writeScanner(t, scannerWrites(
		fixtureFile{"b.spdx.json", runnerStatement},
		fixtureFile{"a.spdx.json", runnerStatement},
		fixtureFile{"nested/ignored.spdx.json", runnerStatement},
		fixtureFile{"a.txt", "not an SPDX statement"},
	))

	if err := Run(context.Background(), Config{
		ScannerPath: scanner,
		Destination: destination,
		MaxBytes:    compact.DefaultMaxBytes,
	}); err != nil {
		t.Fatalf("Run() error = %v", err)
	}
	for _, name := range []string{"a.spdx.json", "b.spdx.json"} {
		assertCompactedStatement(t, filepath.Join(destination, name))
	}
	if _, err := os.Stat(filepath.Join(destination, "nested", "ignored.spdx.json")); err != nil {
		t.Fatalf("nested fixture was not created: %v", err)
	}
}

func TestRunDoesNotModifyOutputWhenAnyDocumentIsInvalid(t *testing.T) {
	requireUnix(t)
	destination := t.TempDir()
	withScannerDestination(t, destination)
	validOriginal := []byte(runnerStatement + "\n")
	invalidOriginal := []byte(`{"predicateType":"https://spdx.dev/Document","predicate":` + "\n")
	scanner := writeScanner(t, scannerWrites(
		fixtureFile{"a.spdx.json", runnerStatement},
		fixtureFile{"b.spdx.json", strings.TrimSuffix(string(invalidOriginal), "\n")},
	))

	err := Run(context.Background(), Config{
		ScannerPath: scanner,
		Destination: destination,
		MaxBytes:    compact.DefaultMaxBytes,
	})
	if err == nil {
		t.Fatal("Run() error = nil, want invalid-document error")
	}
	for _, fixture := range []fixtureFile{
		{name: "a.spdx.json", contents: string(validOriginal)},
		{name: "b.spdx.json", contents: string(invalidOriginal)},
	} {
		got, readErr := os.ReadFile(filepath.Join(destination, fixture.name))
		if readErr != nil {
			t.Fatalf("read %s after failed Run(): %v", fixture.name, readErr)
		}
		if !bytes.Equal(got, []byte(fixture.contents)) {
			t.Fatalf("%s changed after failed Run(): got %q, want %q", fixture.name, got, fixture.contents)
		}
	}
}

type fixtureFile struct {
	name     string
	contents string
}

func requireUnix(t *testing.T) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("scanner fixtures use Unix shell executables")
	}
}

func withScannerDestination(t *testing.T, destination string) {
	t.Helper()
	previous, wasSet := os.LookupEnv("BUILDKIT_SCAN_DESTINATION")
	if err := os.Setenv("BUILDKIT_SCAN_DESTINATION", destination); err != nil {
		t.Fatalf("set scanner destination: %v", err)
	}
	t.Cleanup(func() {
		if wasSet {
			_ = os.Setenv("BUILDKIT_SCAN_DESTINATION", previous)
		} else {
			_ = os.Unsetenv("BUILDKIT_SCAN_DESTINATION")
		}
	})
}

func writeScanner(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "scanner.sh")
	script := "#!/bin/sh\nset -eu\n" + body + "\n"
	if err := os.WriteFile(path, []byte(script), 0o700); err != nil {
		t.Fatalf("write scanner fixture: %v", err)
	}
	return path
}

func scannerWrites(fixtures ...fixtureFile) string {
	var builder strings.Builder
	for _, fixture := range fixtures {
		if strings.Contains(fixture.name, "/") {
			builder.WriteString(fmt.Sprintf("mkdir -p %q\n", filepath.Dir(filepath.Join("$BUILDKIT_SCAN_DESTINATION", fixture.name))))
		}
		builder.WriteString(fmt.Sprintf("cat > %q <<'EOF'\n%s\nEOF\n", filepath.Join("$BUILDKIT_SCAN_DESTINATION", fixture.name), fixture.contents))
	}
	return builder.String()
}

func assertCompactedStatement(t *testing.T, path string) {
	t.Helper()
	output, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read compacted output %s: %v", path, err)
	}
	if bytes.Contains(output, []byte(`"files"`)) {
		t.Fatalf("compacted output %s still contains files: %s", path, output)
	}
	if !bytes.Contains(output, []byte(`"runner-package"`)) {
		t.Fatalf("compacted output %s lost package data: %s", path, output)
	}
	if !bytes.HasSuffix(output, []byte("\n")) {
		t.Fatalf("compacted output %s has no trailing newline", path)
	}
	if _, err := compact.Statement(output, compact.DefaultMaxBytes); err != nil {
		t.Fatalf("compacted output %s is not a valid statement: %v", path, err)
	}
	var statement map[string]json.RawMessage
	if err := json.Unmarshal(bytes.TrimSuffix(output, []byte("\n")), &statement); err != nil {
		t.Fatalf("decode compacted output %s: %v", path, err)
	}
	if !bytes.Contains(statement["predicate"], []byte(`"SPDXRef-Unknown"`)) {
		t.Fatalf("compacted output %s lost unknown hasFiles reference", path)
	}
}
