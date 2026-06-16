# rsync usage

---

Copy syncronised directory to github, including raster files: 

```
rsync -av --update \
  atlas/ sconstruct atlas_master.xlsx \
  "/path/to/Dropbox/atlas-backup/"
```

Bring any updated tiepoint files back to pwd:

```
rsync -av --update \
  --include='*/' --include='*.gpkg' --exclude='*' \
  "/path/to/Dropbox/atlas-backup/" .
  ```