package compact

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

const DefaultMaxBytes int64 = 32 << 20

const spdxPredicate = "https://spdx.dev/Document"

// Statement removes file-level SPDX data from an in-toto SPDX statement while
// retaining every package and relationship that is not proven to refer to a
// removed file.
func Statement(input []byte, maxBytes int64) ([]byte, error) {
	if maxBytes <= 0 {
		return nil, fmt.Errorf("maximum size must be positive")
	}

	statement, err := decodeObject(input, "statement")
	if err != nil {
		return nil, err
	}
	predicateType, err := stringField(statement, "predicateType", "statement")
	if err != nil {
		return nil, err
	}
	if predicateType != spdxPredicate {
		return nil, fmt.Errorf("statement predicateType is not SPDX")
	}
	predicateRaw, ok := statement["predicate"]
	if !ok {
		return nil, fmt.Errorf("statement predicate is missing")
	}
	predicate, err := decodeObject(predicateRaw, "predicate")
	if err != nil {
		return nil, err
	}

	var fileIDs map[string]json.RawMessage
	if filesRaw, ok := predicate["files"]; ok {
		files, err := decodeObjectArray(filesRaw, "predicate.files")
		if err != nil {
			return nil, err
		}
		fileIDs, err = collectFileIDs(files)
		if err != nil {
			return nil, err
		}
		delete(predicate, "files")
	} else {
		fileIDs = make(map[string]json.RawMessage)
	}

	if packagesRaw, ok := predicate["packages"]; ok {
		packages, err := decodeObjectArray(packagesRaw, "predicate.packages")
		if err != nil {
			return nil, err
		}
		if err := filterHasFiles(packages, fileIDs); err != nil {
			return nil, err
		}
		encoded, err := json.Marshal(packages)
		if err != nil {
			return nil, fmt.Errorf("marshal predicate.packages: %w", err)
		}
		predicate["packages"] = encoded
	}

	if relationshipsRaw, ok := predicate["relationships"]; ok {
		relationships, err := decodeObjectArray(relationshipsRaw, "predicate.relationships")
		if err != nil {
			return nil, err
		}
		relationships, err = filterRelationships(relationships, fileIDs)
		if err != nil {
			return nil, err
		}
		encoded, err := json.Marshal(relationships)
		if err != nil {
			return nil, fmt.Errorf("marshal predicate.relationships: %w", err)
		}
		predicate["relationships"] = encoded
	}

	encodedPredicate, err := json.Marshal(predicate)
	if err != nil {
		return nil, fmt.Errorf("marshal predicate: %w", err)
	}
	statement["predicate"] = encodedPredicate
	output, err := json.Marshal(statement)
	if err != nil {
		return nil, fmt.Errorf("marshal statement: %w", err)
	}
	output = append(output, '\n')
	if int64(len(output)) > maxBytes {
		return nil, fmt.Errorf("compacted statement exceeds maximum size %d bytes", maxBytes)
	}
	return output, nil
}

// File compacts path and atomically replaces it after the replacement has
// been completely written, synchronized, and closed.
func File(path string, maxBytes int64) error {
	info, err := os.Stat(path)
	if err != nil {
		return fmt.Errorf("stat statement %q: %w", path, err)
	}
	original, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read statement %q: %w", path, err)
	}
	output, err := Statement(original, maxBytes)
	if err != nil {
		return fmt.Errorf("compact statement %q: %w", path, err)
	}

	directory := filepath.Dir(path)
	temporary, err := os.CreateTemp(directory, "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return fmt.Errorf("create temporary statement for %q: %w", path, err)
	}
	temporaryPath := temporary.Name()
	replaced := false
	defer func() {
		if !replaced {
			_ = temporary.Close()
			_ = os.Remove(temporaryPath)
		}
	}()

	if err := temporary.Chmod(info.Mode()); err != nil {
		return fmt.Errorf("apply statement mode for %q: %w", path, err)
	}
	if _, err := temporary.Write(output); err != nil {
		return fmt.Errorf("write temporary statement for %q: %w", path, err)
	}
	if err := temporary.Sync(); err != nil {
		return fmt.Errorf("sync temporary statement for %q: %w", path, err)
	}
	if err := temporary.Close(); err != nil {
		return fmt.Errorf("close temporary statement for %q: %w", path, err)
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return fmt.Errorf("rename temporary statement for %q: %w", path, err)
	}
	replaced = true
	return nil
}

func decodeObject(raw []byte, label string) (map[string]json.RawMessage, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	var object map[string]json.RawMessage
	if err := decoder.Decode(&object); err != nil {
		return nil, fmt.Errorf("decode %s as object: %w", label, err)
	}
	if object == nil {
		return nil, fmt.Errorf("decode %s as object: value is not an object", label)
	}
	if err := requireEOF(decoder); err != nil {
		return nil, fmt.Errorf("decode %s: %w", label, err)
	}
	return object, nil
}

func decodeObjectArray(raw json.RawMessage, label string) ([]map[string]json.RawMessage, error) {
	values, err := decodeRawArray(raw, label)
	if err != nil {
		return nil, err
	}
	objects := make([]map[string]json.RawMessage, 0, len(values))
	for index, value := range values {
		object, err := decodeObject(value, fmt.Sprintf("%s[%d]", label, index))
		if err != nil {
			return nil, err
		}
		objects = append(objects, object)
	}
	return objects, nil
}

func decodeRawArray(raw []byte, label string) ([]json.RawMessage, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	var values []json.RawMessage
	if err := decoder.Decode(&values); err != nil {
		return nil, fmt.Errorf("decode %s as array: %w", label, err)
	}
	if values == nil {
		return nil, fmt.Errorf("decode %s as array: value is not an array", label)
	}
	if err := requireEOF(decoder); err != nil {
		return nil, fmt.Errorf("decode %s: %w", label, err)
	}
	return values, nil
}

func requireEOF(decoder *json.Decoder) error {
	var extra json.RawMessage
	if err := decoder.Decode(&extra); err != io.EOF {
		if err == nil {
			return fmt.Errorf("trailing JSON value")
		}
		return fmt.Errorf("trailing JSON: %w", err)
	}
	return nil
}

func stringField(obj map[string]json.RawMessage, field, label string) (string, error) {
	raw, ok := obj[field]
	if !ok {
		return "", fmt.Errorf("%s field %q is missing", label, field)
	}
	if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		return "", fmt.Errorf("%s field %q must be a string", label, field)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	var value string
	if err := decoder.Decode(&value); err != nil {
		return "", fmt.Errorf("%s field %q must be a string: %w", label, field, err)
	}
	if err := requireEOF(decoder); err != nil {
		return "", fmt.Errorf("%s field %q: %w", label, field, err)
	}
	return value, nil
}

func collectFileIDs(files []map[string]json.RawMessage) (map[string]json.RawMessage, error) {
	fileIDs := make(map[string]json.RawMessage, len(files))
	for index, file := range files {
		label := fmt.Sprintf("predicate.files[%d]", index)
		id, err := stringField(file, "SPDXID", label)
		if err != nil {
			return nil, err
		}
		if id == "" {
			return nil, fmt.Errorf("%s field %q must not be empty", label, "SPDXID")
		}
		canonical, err := json.Marshal(file)
		if err != nil {
			return nil, fmt.Errorf("marshal %s: %w", label, err)
		}
		if previous, ok := fileIDs[id]; ok && !bytes.Equal(previous, canonical) {
			return nil, fmt.Errorf("duplicate file SPDXID has conflicting content")
		}
		fileIDs[id] = canonical
	}
	return fileIDs, nil
}

func filterHasFiles(packages []map[string]json.RawMessage, fileIDs map[string]json.RawMessage) error {
	for index, pkg := range packages {
		raw, ok := pkg["hasFiles"]
		if !ok {
			continue
		}
		label := fmt.Sprintf("predicate.packages[%d].hasFiles", index)
		values, err := decodeRawArray(raw, label)
		if err != nil {
			return err
		}
		retained := make([]json.RawMessage, 0, len(values))
		for valueIndex, value := range values {
			if bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
				return fmt.Errorf("%s[%d] must be a string", label, valueIndex)
			}
			decoder := json.NewDecoder(bytes.NewReader(value))
			var id string
			if err := decoder.Decode(&id); err != nil {
				return fmt.Errorf("%s[%d] must be a string: %w", label, valueIndex, err)
			}
			if err := requireEOF(decoder); err != nil {
				return fmt.Errorf("%s[%d]: %w", label, valueIndex, err)
			}
			if _, removed := fileIDs[id]; !removed || len(fileIDs) == 0 {
				retained = append(retained, value)
			}
		}
		if len(fileIDs) == 0 {
			continue
		}
		if len(retained) == 0 {
			delete(pkg, "hasFiles")
			continue
		}
		encoded, err := json.Marshal(retained)
		if err != nil {
			return fmt.Errorf("marshal %s: %w", label, err)
		}
		pkg["hasFiles"] = encoded
	}
	return nil
}

func filterRelationships(relationships []map[string]json.RawMessage, fileIDs map[string]json.RawMessage) ([]map[string]json.RawMessage, error) {
	filtered := make([]map[string]json.RawMessage, 0, len(relationships))
	for index, relationship := range relationships {
		label := fmt.Sprintf("predicate.relationships[%d]", index)
		from, err := stringField(relationship, "spdxElementId", label)
		if err != nil {
			return nil, err
		}
		to, err := stringField(relationship, "relatedSpdxElement", label)
		if err != nil {
			return nil, err
		}
		if _, removed := fileIDs[from]; removed {
			continue
		}
		if _, removed := fileIDs[to]; removed {
			continue
		}
		filtered = append(filtered, relationship)
	}
	return filtered, nil
}
