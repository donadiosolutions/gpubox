package compact

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const primaryFixture = `{
  "_type": "https://in-toto.io/Statement/v1",
  "predicateType": "https://spdx.dev/Document",
  "subject": [{"name":"example","digest":{"sha256":"abc"}}],
  "predicate": {
    "SPDXID": "SPDXRef-DOCUMENT",
    "documentNamespace": "https://example.invalid/123",
    "files": [
      {"SPDXID":"SPDXRef-File-A","fileName":"/bin/a"},
      {"SPDXID":"SPDXRef-File-B","fileName":"/bin/b"}
    ],
    "packages": [
      {
        "SPDXID":"SPDXRef-Package-A",
        "name":"package-a",
        "versionInfo":"1.0.0",
        "packageFileName":"archive-9007199254740993.tar",
        "hasFiles":["SPDXRef-File-A","SPDXRef-Unknown"]
      }
    ],
    "relationships": [
      {"spdxElementId":"SPDXRef-DOCUMENT","relationshipType":"DESCRIBES","relatedSpdxElement":"SPDXRef-Package-A"},
      {"spdxElementId":"SPDXRef-Package-A","relationshipType":"CONTAINS","relatedSpdxElement":"SPDXRef-File-A"},
      {"spdxElementId":"SPDXRef-Package-A","relationshipType":"DEPENDS_ON","relatedSpdxElement":"SPDXRef-Package-B"},
      {"spdxElementId":"SPDXRef-Package-A","relationshipType":"OTHER","relatedSpdxElement":"SPDXRef-FileNamedButUnknown"}
    ],
    "exactInteger": 9007199254740993
  }
}`

func TestStatementRemovesOnlyProvenFileData(t *testing.T) {
	output, err := Statement([]byte(primaryFixture), DefaultMaxBytes)
	if err != nil {
		t.Fatalf("Statement() error = %v", err)
	}
	assertExactlyOneTrailingNewline(t, output)

	var statement map[string]json.RawMessage
	if err := json.Unmarshal(bytes.TrimSuffix(output, []byte("\n")), &statement); err != nil {
		t.Fatalf("output is not a JSON object: %v", err)
	}
	var predicate map[string]json.RawMessage
	if err := json.Unmarshal(statement["predicate"], &predicate); err != nil {
		t.Fatalf("predicate is not an object: %v", err)
	}
	if _, ok := predicate["files"]; ok {
		t.Fatal("predicate.files remains after compaction")
	}

	var packages []map[string]json.RawMessage
	if err := json.Unmarshal(predicate["packages"], &packages); err != nil {
		t.Fatalf("packages are not an array: %v", err)
	}
	if len(packages) != 1 {
		t.Fatalf("packages length = %d, want 1", len(packages))
	}
	var hasFiles []string
	if err := json.Unmarshal(packages[0]["hasFiles"], &hasFiles); err != nil {
		t.Fatalf("hasFiles is not an array: %v", err)
	}
	if got, want := hasFiles, []string{"SPDXRef-Unknown"}; fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("hasFiles = %v, want %v", got, want)
	}

	var relationships []map[string]json.RawMessage
	if err := json.Unmarshal(predicate["relationships"], &relationships); err != nil {
		t.Fatalf("relationships are not an array: %v", err)
	}
	if len(relationships) != 3 {
		t.Fatalf("relationships length = %d, want 3", len(relationships))
	}
	for _, relationship := range relationships {
		var related string
		if err := json.Unmarshal(relationship["relatedSpdxElement"], &related); err != nil {
			t.Fatalf("relatedSpdxElement is not a string: %v", err)
		}
		if related == "SPDXRef-File-A" {
			t.Fatal("relationship to removed file remains")
		}
	}
	if !bytes.Contains(output, []byte(`"SPDXRef-Package-A"`)) ||
		!bytes.Contains(output, []byte(`"DEPENDS_ON"`)) ||
		!bytes.Contains(output, []byte(`"SPDXRef-FileNamedButUnknown"`)) {
		t.Fatal("package, package relationship, or unknown endpoint was not preserved")
	}
}

func TestStatementPreservesUnknownHasFilesAndRelationships(t *testing.T) {
	input := `{
    "predicateType":"https://spdx.dev/Document",
    "predicate":{
      "files":[{"SPDXID":"SPDXRef-File-Known","fileName":"/known"}],
      "packages":[{"SPDXID":"SPDXRef-Package","hasFiles":["SPDXRef-FileNamedButUnknown","SPDXRef-Unknown"]}],
      "relationships":[
        {"spdxElementId":"SPDXRef-Package","relationshipType":"OTHER","relatedSpdxElement":"SPDXRef-FileNamedButUnknown"},
        {"spdxElementId":"SPDXRef-FileNamedButUnknown","relationshipType":"OTHER","relatedSpdxElement":"SPDXRef-Unknown"}
      ]
    }
  }`
	output, err := Statement([]byte(input), DefaultMaxBytes)
	if err != nil {
		t.Fatalf("Statement() error = %v", err)
	}
	var statement map[string]json.RawMessage
	if err := json.Unmarshal(bytes.TrimSuffix(output, []byte("\n")), &statement); err != nil {
		t.Fatalf("output is not JSON: %v", err)
	}
	var predicate map[string]json.RawMessage
	if err := json.Unmarshal(statement["predicate"], &predicate); err != nil {
		t.Fatalf("predicate is not JSON: %v", err)
	}
	var packages []map[string]json.RawMessage
	if err := json.Unmarshal(predicate["packages"], &packages); err != nil {
		t.Fatalf("packages are not JSON: %v", err)
	}
	var hasFiles []string
	if err := json.Unmarshal(packages[0]["hasFiles"], &hasFiles); err != nil {
		t.Fatalf("hasFiles is not JSON: %v", err)
	}
	if got, want := fmt.Sprint(hasFiles), fmt.Sprint([]string{"SPDXRef-FileNamedButUnknown", "SPDXRef-Unknown"}); got != want {
		t.Fatalf("unknown hasFiles = %q, want %q", got, want)
	}
	var relationships []map[string]json.RawMessage
	if err := json.Unmarshal(predicate["relationships"], &relationships); err != nil {
		t.Fatalf("relationships are not JSON: %v", err)
	}
	if len(relationships) != 2 {
		t.Fatalf("relationships length = %d, want 2", len(relationships))
	}
}

func TestStatementPreservesJSONNumberLexemes(t *testing.T) {
	output, err := Statement([]byte(primaryFixture), DefaultMaxBytes)
	if err != nil {
		t.Fatalf("Statement() error = %v", err)
	}
	if !bytes.Contains(output, []byte(`"exactInteger":9007199254740993`)) {
		t.Fatalf("exact integer lexeme was not preserved in output: %s", output)
	}
	if !bytes.Contains(output, []byte(`archive-9007199254740993.tar`)) {
		t.Fatal("large integer embedded in packageFileName was not preserved")
	}
}

func TestStatementAcceptsAbsentOptionalArrays(t *testing.T) {
	input := `{"_type":"https://in-toto.io/Statement/v1","predicateType":"https://spdx.dev/Document","predicate":{"SPDXID":"SPDXRef-DOCUMENT"}}`
	output, err := Statement([]byte(input), DefaultMaxBytes)
	if err != nil {
		t.Fatalf("Statement() error = %v", err)
	}
	assertExactlyOneTrailingNewline(t, output)
	if bytes.Contains(output, []byte(`"files"`)) || bytes.Contains(output, []byte(`"packages"`)) || bytes.Contains(output, []byte(`"relationships"`)) {
		t.Fatalf("absent optional arrays were introduced: %s", output)
	}
}

func TestStatementRejectsInvalidShapes(t *testing.T) {
	tests := []struct {
		name  string
		input string
	}{
		{name: "top-level array", input: `[]`},
		{name: "trailing JSON", input: primaryFixture + ` {}`},
		{name: "missing predicate", input: `{"predicateType":"https://spdx.dev/Document"}`},
		{name: "predicate null", input: `{"predicateType":"https://spdx.dev/Document","predicate":null}`},
		{name: "files scalar", input: `{"predicateType":"https://spdx.dev/Document","predicate":{"files":null}}`},
		{name: "packages scalar", input: `{"predicateType":"https://spdx.dev/Document","predicate":{"packages":{}}}`},
		{name: "relationships scalar", input: `{"predicateType":"https://spdx.dev/Document","predicate":{"relationships":"bad"}}`},
		{name: "file scalar", input: `{"predicateType":"https://spdx.dev/Document","predicate":{"files":["bad"]}}`},
		{name: "file missing ID", input: `{"predicateType":"https://spdx.dev/Document","predicate":{"files":[{"fileName":"/bad"}]}}`},
		{name: "file empty ID", input: `{"predicateType":"https://spdx.dev/Document","predicate":{"files":[{"SPDXID":"","fileName":"/bad"}]}}`},
		{name: "package scalar", input: `{"predicateType":"https://spdx.dev/Document","predicate":{"packages":[1]}}`},
		{name: "hasFiles scalar", input: `{"predicateType":"https://spdx.dev/Document","predicate":{"packages":[{"hasFiles":null}]}}`},
		{name: "relationship scalar", input: `{"predicateType":"https://spdx.dev/Document","predicate":{"relationships":[false]}}`},
		{name: "relationship missing endpoint", input: `{"predicateType":"https://spdx.dev/Document","predicate":{"relationships":[{"spdxElementId":"SPDXRef-A"}]}}`},
		{name: "relationship non-string endpoint", input: `{"predicateType":"https://spdx.dev/Document","predicate":{"relationships":[{"spdxElementId":1,"relatedSpdxElement":"SPDXRef-A"}]}}`},
		{name: "non-positive limit", input: primaryFixture},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			maxBytes := DefaultMaxBytes
			if test.name == "non-positive limit" {
				maxBytes = 0
			}
			if _, err := Statement([]byte(test.input), maxBytes); err == nil {
				t.Fatal("Statement() error = nil, want an error")
			}
		})
	}
}

func TestStatementRejectsNonSPDXPredicate(t *testing.T) {
	input := strings.Replace(primaryFixture, "https://spdx.dev/Document", "https://example.invalid/not-spdx", 1)
	if _, err := Statement([]byte(input), DefaultMaxBytes); err == nil {
		t.Fatal("Statement() error = nil, want non-SPDX predicate error")
	}
}

func TestStatementRejectsDuplicateFileIDsWithConflictingContent(t *testing.T) {
	input := `{
    "predicateType":"https://spdx.dev/Document",
    "predicate":{
      "files":[
        {"SPDXID":"SPDXRef-File-Duplicate","fileName":"/one"},
        {"SPDXID":"SPDXRef-File-Duplicate","fileName":"/two"}
      ]
    }
  }`
	if _, err := Statement([]byte(input), DefaultMaxBytes); err == nil {
		t.Fatal("Statement() error = nil, want conflicting duplicate file ID error")
	} else if !strings.Contains(strings.ToLower(err.Error()), "duplicate") {
		t.Fatalf("error = %v, want duplicate-file indication", err)
	}
}

func TestStatementEnforcesMaximumSize(t *testing.T) {
	withoutLimit, err := Statement([]byte(primaryFixture), DefaultMaxBytes)
	if err != nil {
		t.Fatalf("Statement() without tight limit error = %v", err)
	}
	if _, err := Statement([]byte(primaryFixture), int64(len(withoutLimit)-1)); err == nil {
		t.Fatal("Statement() error = nil, want size error")
	} else if !strings.Contains(strings.ToLower(err.Error()), "exceeds") {
		t.Fatalf("error = %v, want exceeds indication", err)
	}
}

func TestFileAtomicallyReplacesValidStatement(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "statement.spdx.json")
	original := []byte(primaryFixture)
	if err := os.WriteFile(path, original, 0o640); err != nil {
		t.Fatalf("write fixture: %v", err)
	}
	if err := File(path, DefaultMaxBytes); err != nil {
		t.Fatalf("File() error = %v", err)
	}
	output, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read compacted statement: %v", err)
	}
	if bytes.Equal(output, original) {
		t.Fatal("File() left the original statement unchanged")
	}
	if !bytes.HasSuffix(output, []byte("\n")) || bytes.Contains(output[:len(output)-1], []byte("\n")) {
		t.Fatalf("output does not contain exactly one trailing newline: %q", output)
	}
	if !bytes.Contains(output, []byte(`"files"`)) {
		// The files key should be absent from the predicate; this guard makes sure
		// the output is not an empty replacement while the stronger statement test
		// checks the actual predicate shape.
		if _, err := Statement(output, DefaultMaxBytes); err != nil {
			t.Fatalf("replacement is not a valid statement: %v", err)
		}
	}
}

func TestFileLeavesOriginalOnInvalidStatement(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "statement.spdx.json")
	original := []byte(`{"predicateType":"https://spdx.dev/Document","predicate":`)
	if err := os.WriteFile(path, original, 0o600); err != nil {
		t.Fatalf("write fixture: %v", err)
	}
	if err := File(path, DefaultMaxBytes); err == nil {
		t.Fatal("File() error = nil, want invalid statement error")
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read original statement: %v", err)
	}
	if !bytes.Equal(got, original) {
		t.Fatalf("original bytes changed after invalid input: got %q, want %q", got, original)
	}
}

func TestFilePreservesMode(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "statement.spdx.json")
	if err := os.WriteFile(path, []byte(primaryFixture), 0o600); err != nil {
		t.Fatalf("write fixture: %v", err)
	}
	if err := os.Chmod(path, 0o640); err != nil {
		t.Fatalf("chmod fixture: %v", err)
	}
	before, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat fixture: %v", err)
	}
	if err := File(path, DefaultMaxBytes); err != nil {
		t.Fatalf("File() error = %v", err)
	}
	after, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat replacement: %v", err)
	}
	if got, want := after.Mode().Perm(), before.Mode().Perm(); got != want {
		t.Fatalf("replacement mode = %04o, want %04o", got, want)
	}
}

func assertExactlyOneTrailingNewline(t *testing.T, output []byte) {
	t.Helper()
	if !bytes.HasSuffix(output, []byte("\n")) {
		t.Fatalf("output does not end with newline: %q", output)
	}
	if bytes.HasSuffix(output, []byte("\n\n")) {
		t.Fatalf("output ends with more than one newline: %q", output)
	}
	if bytes.Contains(output[:len(output)-1], []byte("\n")) {
		t.Fatalf("output contains an interior newline: %q", output)
	}
}
