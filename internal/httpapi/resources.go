package httpapi

import (
	"io/fs"
	"os"
	"path/filepath"

	embeddedresource "yyb_go/resource"
)

type resources struct {
	Root      string
	DB        string
	Avatars   string
	QR        string
	Templates string
	Static    string
}

func ensureResources(root string) (resources, error) {
	res := resources{
		Root:      root,
		DB:        filepath.Join(root, "db"),
		Avatars:   filepath.Join(root, "avatars"),
		QR:        filepath.Join(root, "qr"),
		Templates: filepath.Join(root, "templates"),
		Static:    filepath.Join(root, "static"),
	}
	for _, p := range []string{res.DB, res.Avatars, res.QR, res.Templates, filepath.Join(res.Static, "css"), filepath.Join(res.Static, "js")} {
		if err := os.MkdirAll(p, 0o755); err != nil {
			return res, err
		}
	}
	if err := restoreEmbeddedWebAssets(root); err != nil {
		return res, err
	}
	return res, nil
}

func restoreEmbeddedWebAssets(root string) error {
	return fs.WalkDir(embeddedresource.WebAssets, ".", func(assetPath string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if assetPath == "." {
			return nil
		}

		target := filepath.Join(root, filepath.FromSlash(assetPath))
		if entry.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		if _, err := os.Stat(target); err == nil {
			return nil
		} else if !os.IsNotExist(err) {
			return err
		}

		content, err := fs.ReadFile(embeddedresource.WebAssets, assetPath)
		if err != nil {
			return err
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		return os.WriteFile(target, content, 0o644)
	})
}

func (r resources) avatarPath(openid string) string {
	return filepath.Join(r.Avatars, safeName(openid)+".jpg")
}

func (r resources) qrPath(sessionID string) string {
	return filepath.Join(r.QR, sessionID+".jpg")
}
