package main

import (
	"context"
	"fmt"
	"os"

	"github.com/donadiosolutions/gpubox/sbom-generator/internal/compact"
	"github.com/donadiosolutions/gpubox/sbom-generator/internal/runner"
)

func main() {
	destination := os.Getenv("BUILDKIT_SCAN_DESTINATION")
	err := runner.Run(context.Background(), runner.Config{
		ScannerPath: "/bin/syft-scanner",
		Destination: destination,
		MaxBytes:    compact.DefaultMaxBytes,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "gpubox SBOM generator: %v\n", err)
		os.Exit(1)
	}
}
