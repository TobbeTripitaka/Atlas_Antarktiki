# rsync usage

---

Copy syncronised directory to github, including raster files: 

```
rsync -av --update \
  atlas/ \
  "/path/to/Dropbox/atlas-backup/atlas/"

rsync -av --update \
  sconstruct atlas_master.xlsx \
  volume_I.csv volume_II.csv \
  "/path/to/Dropbox/atlas-backup/"
```

Bring any updated tiepoint files back to pwd:

```
rsync -av --update \
  --include='*/' --include='*.gpkg' --exclude='*' \
  "/path/to/Dropbox/atlas-backup/" .
  ```


