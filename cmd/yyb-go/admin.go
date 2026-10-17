package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"golang.org/x/term"
	"yyb_go/internal/auth"
	"yyb_go/internal/httpapi"
)

func resetAdminPassword(cfg httpapi.Config, username string, input *os.File, output io.Writer) error {
	driver, dsn, err := cfg.AuthDatabase()
	if err != nil {
		return err
	}
	if driver != "sqlite" && driver != "mysql" {
		return errors.New("请使用服务实际的 YYB_AUTH_DRIVER（sqlite 或 mysql）及认证数据库配置")
	}
	if driver == "sqlite" {
		path, err := existingAuthFile(dsn)
		if err != nil {
			return err
		}
		fmt.Fprintln(output, "认证数据库：", path)
	} else {
		fmt.Fprintln(output, "认证数据库：MySQL（使用当前配置，不回显连接凭据）")
	}
	password, err := readAdminPassword(input, output)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	store, err := auth.OpenExisting(ctx, driver, dsn)
	if err != nil {
		// Driver errors can contain credentials or DSNs.
		return errors.New("无法连接认证数据库，请核对与运行服务相同的配置及文件权限")
	}
	defer store.Close()
	if err := store.RecoverAdminPassword(ctx, username, password); err != nil {
		return err
	}
	fmt.Fprintln(output, "管理员密码已重置，旧登录会话已注销；微信账号、代理及面板配置保留。")
	return nil
}

func existingAuthFile(dsn string) (string, error) {
	path := strings.SplitN(dsn, "?", 2)[0]
	if strings.HasPrefix(dsn, "file:") {
		parsed, err := url.Parse(dsn)
		if err != nil || (parsed.Host != "" && parsed.Host != "localhost") {
			return "", errors.New("无法识别 SQLite 文件路径，请核对 YYB_AUTH_DSN")
		}
		path = parsed.Path
		if parsed.Opaque != "" {
			path, err = url.PathUnescape(parsed.Opaque)
			if err != nil {
				return "", errors.New("无法识别 SQLite 文件路径，请核对 YYB_AUTH_DSN")
			}
		}
		// file:///C:/... uses an extra slash before the Windows drive.
		if strings.HasPrefix(path, "/") && filepath.VolumeName(path[1:]) != "" {
			path = path[1:]
		}
	}
	path, err := filepath.Abs(path)
	if err != nil {
		return "", errors.New("无法解析认证数据库路径")
	}
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() {
		return "", errors.New("认证数据库文件不存在；请使用与服务相同的工作目录、-resource-root 或 YYB_AUTH_DSN，不会新建数据库")
	}
	return path, nil
}

func readAdminPassword(input *os.File, output io.Writer) (string, error) {
	var password string
	if term.IsTerminal(int(input.Fd())) {
		fmt.Fprint(output, "新密码（不回显）：")
		first, err := term.ReadPassword(int(input.Fd()))
		fmt.Fprintln(output)
		if err != nil {
			return "", errors.New("无法读取新密码")
		}
		fmt.Fprint(output, "再次输入新密码：")
		second, err := term.ReadPassword(int(input.Fd()))
		fmt.Fprintln(output)
		if err != nil || string(first) != string(second) {
			return "", errors.New("两次密码不一致，未修改")
		}
		password = string(first)
	} else {
		line, err := bufio.NewReader(io.LimitReader(input, 130)).ReadString('\n')
		if err != nil && err != io.EOF {
			return "", errors.New("无法从标准输入读取密码")
		}
		password = strings.TrimSuffix(strings.TrimSuffix(line, "\n"), "\r")
	}
	if err := auth.ValidatePassword(password); err != nil {
		return "", err
	}
	if len(password) > 72 {
		return "", errors.New("bcrypt 密码最多为 72 字节，请缩短新密码")
	}
	return password, nil
}
