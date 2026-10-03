package main

import (
	"bytes"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"

	"github.com/joho/godotenv"
)

// Load exactly one file. Existing process variables, including empty values,
// take precedence. Never evaluate shell commands from configuration files.
func loadEnvFile(explicit, executableDir, workingDir string) (string, error) {
	var candidates []string
	if explicit != "" {
		if !filepath.IsAbs(explicit) {
			explicit = filepath.Join(workingDir, explicit)
		}
		candidates = []string{explicit}
	} else {
		if executableDir != "" {
			candidates = append(candidates, filepath.Join(executableDir, ".env"))
		}
		candidates = append(candidates, filepath.Join(workingDir, ".env"))
	}
	for _, path := range candidates {
		content, err := os.ReadFile(path)
		if os.IsNotExist(err) && explicit == "" {
			continue
		}
		if err != nil {
			return "", fmt.Errorf("无法读取配置文件 %q", path)
		}
		content = bytes.TrimPrefix(content, []byte{0xef, 0xbb, 0xbf})
		if !utf8.Valid(content) || bytes.ContainsRune(content, '\x00') {
			return "", fmt.Errorf("配置文件 %q 必须使用 UTF-8 编码", path)
		}
		values, err := godotenv.Unmarshal(string(content))
		if err != nil {
			// Parser errors can contain the full line, including passwords.
			return "", fmt.Errorf("配置文件 %q 格式错误，请检查 KEY=VALUE 和引号是否闭合", path)
		}
		for key, value := range values {
			if key == "" || strings.ContainsAny(key, "=\x00") || strings.ContainsRune(value, '\x00') {
				return "", fmt.Errorf("配置文件 %q 包含无效环境变量", path)
			}
		}
		for key, value := range values {
			if _, exists := os.LookupEnv(key); !exists {
				if err := os.Setenv(key, value); err != nil {
					return "", fmt.Errorf("无法应用配置文件 %q", path)
				}
			}
		}
		return path, nil
	}
	return "", nil
}

func applyEnvFlags(flags *flag.FlagSet) error {
	explicit := map[string]bool{}
	flags.Visit(func(value *flag.Flag) { explicit[value.Name] = true })
	for _, item := range [][2]string{
		{"host", "YYB_BIND_ADDRESS"}, {"port", "YYB_PORT"},
		{"keepalive-interval", "YYB_KEEPALIVE_INTERVAL"}, {"keepalive-ahead", "YYB_KEEPALIVE_AHEAD"},
	} {
		if value := strings.TrimSpace(os.Getenv(item[1])); value != "" && !explicit[item[0]] {
			if err := flags.Set(item[0], value); err != nil {
				return fmt.Errorf("%s 配置无效，请检查值的格式", item[1])
			}
		}
	}
	return nil
}
