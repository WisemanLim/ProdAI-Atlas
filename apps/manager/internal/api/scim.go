package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"atlas-manager/internal/iam"
	"atlas-manager/internal/store"
)

// SCIM 2.0 서버 (FR-PLT-IAM-03, RFC 7643/7644): /scim/v2/Users·Groups. 인증 = 조직 `scim` 범위 API 키(조직별 Bearer).
// 사용자 비활성화·삭제 → 그룹 멤버십 튜플 제거 · 세션 폐기 · 소유 API 키 즉시 폐기. 그룹 → 역할은 scim_group_roles 매핑.

const (
	scimUserSchema  = "urn:ietf:params:scim:schemas:core:2.0:User"
	scimGroupSchema = "urn:ietf:params:scim:schemas:core:2.0:Group"
	scimListSchema  = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
	scimPatchSchema = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
	scimErrSchema   = "urn:ietf:params:scim:api:messages:2.0:Error"
)

func scimFail(c *gin.Context, status int, scimType, detail string) {
	body := gin.H{"schemas": []string{scimErrSchema}, "status": strconv.Itoa(status), "detail": detail}
	if scimType != "" {
		body["scimType"] = scimType
	}
	c.Header("Content-Type", "application/scim+json")
	c.AbortWithStatusJSON(status, body)
}

func scimJSON(c *gin.Context, status int, v any) {
	b, _ := json.Marshal(v)
	c.Data(status, "application/scim+json", b)
}

// scimAuth: 인증 모드에서는 scim 범위 API 키(키의 조직) 또는 플랫폼 관리자(?org=)만.
func (s *Server) scimAuth(c *gin.Context) {
	p := principal(c)
	org := orgOf(c)
	if s.authOn() {
		switch {
		case p.Kind == "apikey" && contains(p.Scopes, "scim"): // 전용 토큰만 — admin 범위가 SCIM 을 겸하지 않는다
			org = p.Org
		case p.PlatformAdmin:
		default:
			scimFail(c, http.StatusUnauthorized, "", "조직 SCIM 토큰(scim 범위 API 키) 필요")
			return
		}
	}
	var o store.Organization
	if s.DB.WithContext(c).First(&o, "slug = ?", org).Error != nil {
		scimFail(c, http.StatusNotFound, "", "조직 없음: "+org)
		return
	}
	c.Set("scim.org", org)
	s.tenantScope(c, org)
}

func scimOrg(c *gin.Context) string { return c.GetString("scim.org") }

type scimEmail struct {
	Value   string `json:"value"`
	Type    string `json:"type,omitempty"`
	Primary bool   `json:"primary,omitempty"`
}

type scimUserBody struct {
	Schemas     []string        `json:"schemas"`
	ExternalID  string          `json:"externalId"`
	UserName    string          `json:"userName"`
	DisplayName string          `json:"displayName"`
	Name        *map[string]any `json:"name"`
	Emails      []scimEmail     `json:"emails"`
	Active      *bool           `json:"active"`
}

func (s *Server) scimLoc(c *gin.Context, kind, id string) string {
	scheme := "http"
	if c.Request.TLS != nil || c.GetHeader("X-Forwarded-Proto") == "https" {
		scheme = "https"
	}
	return scheme + "://" + c.Request.Host + "/scim/v2/" + kind + "/" + id
}

func (s *Server) userResource(c *gin.Context, db *gorm.DB, u store.ScimUser) gin.H {
	var emails []scimEmail
	if u.Emails != nil {
		_ = json.Unmarshal([]byte(*u.Emails), &emails)
	}
	var gs []store.ScimGroup
	db.Where("org_id = ?", u.OrgID).Find(&gs)
	groups := []gin.H{}
	for _, g := range gs {
		if contains(groupMembers(g), u.ID) {
			groups = append(groups, gin.H{"value": g.ID, "display": g.DisplayName, "$ref": s.scimLoc(c, "Groups", g.ID)})
		}
	}
	out := gin.H{"schemas": []string{scimUserSchema}, "id": u.ID, "userName": u.UserName, "active": u.Active, "emails": emails, "groups": groups,
		"meta": gin.H{"resourceType": "User", "created": u.CreatedAt.Format(time.RFC3339), "lastModified": u.UpdatedAt.Format(time.RFC3339),
			"location": s.scimLoc(c, "Users", u.ID), "version": fmt.Sprintf(`W/"%d"`, u.UpdatedAt.UnixNano())}}
	if u.ExternalID != nil {
		out["externalId"] = *u.ExternalID
	}
	if u.DisplayName != nil {
		out["displayName"] = *u.DisplayName
	}
	return out
}

func groupMembers(g store.ScimGroup) []string {
	var ids []string
	_ = json.Unmarshal([]byte(g.Members), &ids)
	return ids
}

func (s *Server) groupResource(c *gin.Context, db *gorm.DB, g store.ScimGroup) gin.H {
	members := []gin.H{}
	for _, id := range groupMembers(g) {
		var u store.ScimUser
		if db.First(&u, "id = ? AND org_id = ?", id, g.OrgID).Error == nil {
			members = append(members, gin.H{"value": u.ID, "display": u.UserName, "$ref": s.scimLoc(c, "Users", u.ID)})
		}
	}
	out := gin.H{"schemas": []string{scimGroupSchema}, "id": g.ID, "displayName": g.DisplayName, "members": members,
		"meta": gin.H{"resourceType": "Group", "created": g.CreatedAt.Format(time.RFC3339), "lastModified": g.UpdatedAt.Format(time.RFC3339),
			"location": s.scimLoc(c, "Groups", g.ID)}}
	if g.ExternalID != nil {
		out["externalId"] = *g.ExternalID
	}
	return out
}

// filter: `attr eq "value"` 만 (IdP 프로비저닝의 존재 확인 용도 — RFC 7644 §3.4.2.2 의 부분집합).
var scimFilterRE = regexp.MustCompile(`^\s*(\w+)\s+eq\s+"([^"]*)"\s*$`)

func scimPage(c *gin.Context) (int, int) {
	start, _ := strconv.Atoi(c.DefaultQuery("startIndex", "1"))
	count, _ := strconv.Atoi(c.DefaultQuery("count", "100"))
	return max(1, start), min(max(0, count), 200)
}

func scimList(c *gin.Context, total int, start int, items []gin.H) {
	scimJSON(c, http.StatusOK, gin.H{"schemas": []string{scimListSchema}, "totalResults": total, "startIndex": start, "itemsPerPage": len(items), "Resources": items})
}

func (s *Server) scimConfig(c *gin.Context) {
	scimJSON(c, http.StatusOK, gin.H{"schemas": []string{"urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"},
		"patch": gin.H{"supported": true}, "bulk": gin.H{"supported": false, "maxOperations": 0, "maxPayloadSize": 0},
		"filter": gin.H{"supported": true, "maxResults": 200}, "changePassword": gin.H{"supported": false}, "sort": gin.H{"supported": false},
		"etag": gin.H{"supported": false}, "authenticationSchemes": []gin.H{{"type": "oauthbearertoken", "name": "OAuth Bearer Token",
			"description": "조직 scim 범위 API 키", "primary": true}}})
}

// ---- Users ----

func (s *Server) scimListUsers(c *gin.Context) {
	db := s.db(c)
	q := db.Model(&store.ScimUser{}).Where("org_id = ?", scimOrg(c))
	if f := c.Query("filter"); f != "" {
		m := scimFilterRE.FindStringSubmatch(f)
		col := map[string]string{"userName": "user_name", "externalId": "external_id", "id": "id"}
		if m == nil || col[m[1]] == "" {
			scimFail(c, http.StatusBadRequest, "invalidFilter", "지원 filter: userName|externalId|id eq \"…\"")
			return
		}
		q = q.Where(col[m[1]]+" = ?", m[2])
	}
	var total int64
	q.Count(&total)
	start, count := scimPage(c)
	var us []store.ScimUser
	q.Order("created_at, id").Offset(start - 1).Limit(count).Find(&us)
	items := make([]gin.H, 0, len(us))
	for _, u := range us {
		items = append(items, s.userResource(c, db, u))
	}
	scimList(c, int(total), start, items)
}

func (s *Server) scimGetUser(c *gin.Context) {
	db := s.db(c)
	var u store.ScimUser
	if db.First(&u, "id = ? AND org_id = ?", c.Param("id"), scimOrg(c)).Error != nil {
		scimFail(c, http.StatusNotFound, "", "사용자 없음")
		return
	}
	scimJSON(c, http.StatusOK, s.userResource(c, db, u))
}

func applyUserBody(u *store.ScimUser, b scimUserBody) {
	u.UserName = b.UserName
	u.ExternalID, u.DisplayName = optStr(b.ExternalID), optStr(b.DisplayName)
	if raw, _ := json.Marshal(b.Emails); len(b.Emails) > 0 {
		v := string(raw)
		u.Emails = &v
	} else {
		u.Emails = nil
	}
	if b.Active != nil {
		u.Active = *b.Active
	}
}

func optStr(v string) *string {
	if v == "" {
		return nil
	}
	return &v
}

func (s *Server) scimCreateUser(c *gin.Context) {
	var b scimUserBody
	if err := c.ShouldBindJSON(&b); err != nil || strings.TrimSpace(b.UserName) == "" {
		scimFail(c, http.StatusBadRequest, "invalidValue", "userName 필수")
		return
	}
	db, org := s.db(c), scimOrg(c)
	var n int64
	db.Model(&store.ScimUser{}).Where("org_id = ? AND user_name = ?", org, b.UserName).Count(&n)
	if n > 0 {
		scimFail(c, http.StatusConflict, "uniqueness", "userName 중복: "+b.UserName)
		return
	}
	now := time.Now().UTC()
	u := store.ScimUser{ID: iam.ULID(), OrgID: org, Active: true, CreatedAt: now, UpdatedAt: now}
	applyUserBody(&u, b)
	if err := db.Create(&u).Error; err != nil {
		scimFail(c, http.StatusInternalServerError, "", err.Error())
		return
	}
	s.audit(c, org, "scim.user.create", "scim-user:"+u.ID, "success", nil, gin.H{"userName": u.UserName, "subject": u.Subject()}, nil)
	scimJSON(c, http.StatusCreated, s.userResource(c, db, u))
}

func (s *Server) scimReplaceUser(c *gin.Context) {
	var b scimUserBody
	if err := c.ShouldBindJSON(&b); err != nil || strings.TrimSpace(b.UserName) == "" {
		scimFail(c, http.StatusBadRequest, "invalidValue", "userName 필수")
		return
	}
	s.mutateUser(c, func(u *store.ScimUser) error {
		if b.Active == nil {
			t := true
			b.Active = &t // PUT 은 전체 교체 — 생략된 active 는 기본값 true
		}
		applyUserBody(u, b)
		return nil
	})
}

type scimPatch struct {
	Schemas    []string `json:"schemas"`
	Operations []struct {
		Op    string          `json:"op"`
		Path  string          `json:"path"`
		Value json.RawMessage `json:"value"`
	} `json:"Operations"`
}

func (s *Server) scimPatchUser(c *gin.Context) {
	var p scimPatch
	if err := c.ShouldBindJSON(&p); err != nil || len(p.Operations) == 0 {
		scimFail(c, http.StatusBadRequest, "invalidSyntax", "PatchOp Operations 필요")
		return
	}
	s.mutateUser(c, func(u *store.ScimUser) error {
		for _, op := range p.Operations {
			if o := strings.ToLower(op.Op); o != "replace" && o != "add" {
				return fmt.Errorf("사용자 PATCH 는 replace·add 만")
			}
			vals := map[string]json.RawMessage{}
			if op.Path != "" {
				vals[op.Path] = op.Value
			} else if json.Unmarshal(op.Value, &vals) != nil {
				return fmt.Errorf("path 없는 op 의 value 는 객체")
			}
			for k, v := range vals {
				switch k {
				case "active":
					var b bool
					if json.Unmarshal(v, &b) != nil { // 일부 IdP(Entra)는 "False" 문자열
						var str string
						_ = json.Unmarshal(v, &str)
						b = strings.EqualFold(str, "true")
					}
					u.Active = b
				case "userName":
					_ = json.Unmarshal(v, &u.UserName)
				case "displayName":
					var str string
					_ = json.Unmarshal(v, &str)
					u.DisplayName = optStr(str)
				case "externalId":
					var str string
					_ = json.Unmarshal(v, &str)
					u.ExternalID = optStr(str)
				case "emails":
					str := string(v)
					u.Emails = &str
				}
			}
		}
		return nil
	})
}

// mutateUser 는 사용자를 바꾸고 활성 상태 전이를 반영한다(비활성화 = 즉시 접근 회수).
func (s *Server) mutateUser(c *gin.Context, fn func(*store.ScimUser) error) {
	db, org := s.db(c), scimOrg(c)
	var u store.ScimUser
	if db.First(&u, "id = ? AND org_id = ?", c.Param("id"), org).Error != nil {
		scimFail(c, http.StatusNotFound, "", "사용자 없음")
		return
	}
	before := u
	if err := fn(&u); err != nil {
		scimFail(c, http.StatusBadRequest, "invalidValue", err.Error())
		return
	}
	u.UpdatedAt = time.Now().UTC()
	if err := db.Save(&u).Error; err != nil {
		scimFail(c, http.StatusConflict, "uniqueness", err.Error())
		return
	}
	switch {
	case before.Active && !u.Active:
		s.deprovision(c, db, before)
	case !before.Active && u.Active:
		s.syncMemberships(c, db, u, true)
		s.audit(c, org, "scim.user.reactivate", "scim-user:"+u.ID, "success", nil, nil, nil)
	}
	scimJSON(c, http.StatusOK, s.userResource(c, db, u))
}

func (s *Server) scimDeleteUser(c *gin.Context) {
	db, org := s.db(c), scimOrg(c)
	var u store.ScimUser
	if db.First(&u, "id = ? AND org_id = ?", c.Param("id"), org).Error != nil {
		scimFail(c, http.StatusNotFound, "", "사용자 없음")
		return
	}
	if u.Active {
		s.deprovision(c, db, u)
	}
	var gs []store.ScimGroup
	db.Where("org_id = ?", org).Find(&gs)
	for _, g := range gs {
		if ms := groupMembers(g); contains(ms, u.ID) {
			g.Members = mustJSON(remove(ms, u.ID))
			db.Save(&g)
		}
	}
	db.Delete(&u)
	c.Status(http.StatusNoContent)
}

func remove(xs []string, v string) []string {
	out := []string{}
	for _, x := range xs {
		if x != v {
			out = append(out, x)
		}
	}
	return out
}

func mustJSON(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

// deprovision: 그룹 멤버십 튜플 제거 · 세션 폐기(이전 발급 토큰 무효) · 소유 API 키 폐기 · 이벤트.
func (s *Server) deprovision(c *gin.Context, db *gorm.DB, u store.ScimUser) {
	org, now, sub := u.OrgID, time.Now().UTC(), u.Subject()
	s.syncMemberships(c, db, u, false)
	s.DB.WithContext(c).Save(&store.SessionRevocation{Sub: sub, OrgID: org, RevokedAt: now})
	s.DB.WithContext(c).Model(&store.AuthSession{}).Where("sub = ?", sub).Update("revoked", true)
	owners := []string{u.UserName, sub}
	res := db.Model(&store.APIKey{}).Where("org_id = ? AND owner IN ? AND revoked_at IS NULL", org, owners).Update("revoked_at", now)
	s.audit(c, org, "scim.user.deprovision", "scim-user:"+u.ID, "success", nil, nil, map[string]any{"subject": sub, "apiKeysRevoked": res.RowsAffected})
	s.emit(c, "plt.iam.user_deprovisioned", gin.H{"org": org, "user": u.UserName, "subject": sub, "apiKeysRevoked": res.RowsAffected})
}

// syncMemberships 는 사용자가 속한 모든 그룹의 member 튜플을 쓰거나 지운다.
func (s *Server) syncMemberships(c *gin.Context, db *gorm.DB, u store.ScimUser, add bool) {
	if s.FGA == nil {
		return
	}
	var gs []store.ScimGroup
	db.Where("org_id = ?", u.OrgID).Find(&gs)
	var ts []iam.Tuple
	for _, g := range gs {
		if contains(groupMembers(g), u.ID) {
			ts = append(ts, iam.Tuple{User: "user:" + u.Subject(), Relation: "member", Object: groupObj(u.OrgID, g.ID)})
		}
	}
	if add {
		_ = s.FGA.Write(c, ts, nil)
	} else {
		_ = s.FGA.Write(c, nil, ts)
	}
}

func groupObj(org, gid string) string { return "group:" + org + "/" + gid }

// ---- Groups ----

type scimGroupBody struct {
	Schemas     []string `json:"schemas"`
	DisplayName string   `json:"displayName"`
	ExternalID  string   `json:"externalId"`
	Members     []struct {
		Value string `json:"value"`
	} `json:"members"`
}

func (s *Server) scimListGroups(c *gin.Context) {
	db := s.db(c)
	q := db.Model(&store.ScimGroup{}).Where("org_id = ?", scimOrg(c))
	if f := c.Query("filter"); f != "" {
		m := scimFilterRE.FindStringSubmatch(f)
		col := map[string]string{"displayName": "display_name", "externalId": "external_id", "id": "id"}
		if m == nil || col[m[1]] == "" {
			scimFail(c, http.StatusBadRequest, "invalidFilter", "지원 filter: displayName|externalId|id eq \"…\"")
			return
		}
		q = q.Where(col[m[1]]+" = ?", m[2])
	}
	var total int64
	q.Count(&total)
	start, count := scimPage(c)
	var gs []store.ScimGroup
	q.Order("created_at, id").Offset(start - 1).Limit(count).Find(&gs)
	items := make([]gin.H, 0, len(gs))
	for _, g := range gs {
		items = append(items, s.groupResource(c, db, g))
	}
	scimList(c, int(total), start, items)
}

func (s *Server) scimGetGroup(c *gin.Context) {
	db := s.db(c)
	var g store.ScimGroup
	if db.First(&g, "id = ? AND org_id = ?", c.Param("id"), scimOrg(c)).Error != nil {
		scimFail(c, http.StatusNotFound, "", "그룹 없음")
		return
	}
	scimJSON(c, http.StatusOK, s.groupResource(c, db, g))
}

func (s *Server) scimCreateGroup(c *gin.Context) {
	var b scimGroupBody
	if err := c.ShouldBindJSON(&b); err != nil || strings.TrimSpace(b.DisplayName) == "" {
		scimFail(c, http.StatusBadRequest, "invalidValue", "displayName 필수")
		return
	}
	db, org := s.db(c), scimOrg(c)
	var n int64
	db.Model(&store.ScimGroup{}).Where("org_id = ? AND display_name = ?", org, b.DisplayName).Count(&n)
	if n > 0 {
		scimFail(c, http.StatusConflict, "uniqueness", "displayName 중복: "+b.DisplayName)
		return
	}
	now := time.Now().UTC()
	g := store.ScimGroup{ID: iam.ULID(), OrgID: org, DisplayName: b.DisplayName, ExternalID: optStr(b.ExternalID), Members: "[]", CreatedAt: now, UpdatedAt: now}
	if err := db.Create(&g).Error; err != nil {
		scimFail(c, http.StatusInternalServerError, "", err.Error())
		return
	}
	s.syncGroupRoles(c, db, g, true)
	ids := make([]string, 0, len(b.Members))
	for _, m := range b.Members {
		ids = append(ids, m.Value)
	}
	s.setMembers(c, db, &g, ids)
	scimJSON(c, http.StatusCreated, s.groupResource(c, db, g))
}

func (s *Server) scimReplaceGroup(c *gin.Context) {
	var b scimGroupBody
	if err := c.ShouldBindJSON(&b); err != nil || strings.TrimSpace(b.DisplayName) == "" {
		scimFail(c, http.StatusBadRequest, "invalidValue", "displayName 필수")
		return
	}
	db, org := s.db(c), scimOrg(c)
	var g store.ScimGroup
	if db.First(&g, "id = ? AND org_id = ?", c.Param("id"), org).Error != nil {
		scimFail(c, http.StatusNotFound, "", "그룹 없음")
		return
	}
	if g.DisplayName != b.DisplayName { // 이름이 바뀌면 역할 매핑도 새 이름 기준
		s.syncGroupRoles(c, db, g, false)
		g.DisplayName = b.DisplayName
		s.syncGroupRoles(c, db, g, true)
	}
	g.ExternalID = optStr(b.ExternalID)
	ids := make([]string, 0, len(b.Members))
	for _, m := range b.Members {
		ids = append(ids, m.Value)
	}
	s.setMembers(c, db, &g, ids)
	scimJSON(c, http.StatusOK, s.groupResource(c, db, g))
}

// memberPathRE: `members[value eq "id"]` (Entra·Okta 의 remove 형식)
var memberPathRE = regexp.MustCompile(`^members\[value eq "([^"]+)"\]$`)

func (s *Server) scimPatchGroup(c *gin.Context) {
	var p scimPatch
	if err := c.ShouldBindJSON(&p); err != nil || len(p.Operations) == 0 {
		scimFail(c, http.StatusBadRequest, "invalidSyntax", "PatchOp Operations 필요")
		return
	}
	db, org := s.db(c), scimOrg(c)
	var g store.ScimGroup
	if db.First(&g, "id = ? AND org_id = ?", c.Param("id"), org).Error != nil {
		scimFail(c, http.StatusNotFound, "", "그룹 없음")
		return
	}
	ids := groupMembers(g)
	values := func(raw json.RawMessage) []string {
		var ms []struct {
			Value string `json:"value"`
		}
		_ = json.Unmarshal(raw, &ms)
		out := []string{}
		for _, m := range ms {
			out = append(out, m.Value)
		}
		return out
	}
	for _, op := range p.Operations {
		o := strings.ToLower(op.Op)
		switch {
		case o == "add" && op.Path == "members":
			for _, v := range values(op.Value) {
				if !contains(ids, v) {
					ids = append(ids, v)
				}
			}
		case o == "remove" && op.Path == "members":
			if len(op.Value) == 0 {
				ids = []string{}
			}
			for _, v := range values(op.Value) {
				ids = remove(ids, v)
			}
		case o == "remove" && memberPathRE.MatchString(op.Path):
			ids = remove(ids, memberPathRE.FindStringSubmatch(op.Path)[1])
		case o == "replace" && op.Path == "members":
			ids = values(op.Value)
		case o == "replace" && (op.Path == "displayName" || op.Path == ""):
			var name string
			if op.Path == "" {
				var v struct {
					DisplayName string `json:"displayName"`
				}
				_ = json.Unmarshal(op.Value, &v)
				name = v.DisplayName
			} else {
				_ = json.Unmarshal(op.Value, &name)
			}
			if name != "" && name != g.DisplayName {
				s.syncGroupRoles(c, db, g, false)
				g.DisplayName = name
				s.syncGroupRoles(c, db, g, true)
			}
		default:
			scimFail(c, http.StatusBadRequest, "invalidPath", "지원 op: add|remove|replace members · replace displayName")
			return
		}
	}
	s.setMembers(c, db, &g, ids)
	scimJSON(c, http.StatusOK, s.groupResource(c, db, g))
}

func (s *Server) scimDeleteGroup(c *gin.Context) {
	db, org := s.db(c), scimOrg(c)
	var g store.ScimGroup
	if db.First(&g, "id = ? AND org_id = ?", c.Param("id"), org).Error != nil {
		scimFail(c, http.StatusNotFound, "", "그룹 없음")
		return
	}
	s.setMembers(c, db, &g, nil)
	s.syncGroupRoles(c, db, g, false)
	db.Delete(&g)
	c.Status(http.StatusNoContent)
}

// setMembers 는 멤버 목록을 바꾸고 활성 사용자만 member 튜플 차이를 반영한다.
func (s *Server) setMembers(c *gin.Context, db *gorm.DB, g *store.ScimGroup, ids []string) {
	old := groupMembers(*g)
	valid := []string{}
	for _, id := range ids {
		var n int64
		db.Model(&store.ScimUser{}).Where("id = ? AND org_id = ?", id, g.OrgID).Count(&n)
		if n > 0 && !contains(valid, id) {
			valid = append(valid, id)
		}
	}
	g.Members, g.UpdatedAt = mustJSON(valid), time.Now().UTC()
	db.Save(g)
	if s.FGA == nil {
		return
	}
	var add, del []iam.Tuple
	tuple := func(id string) (iam.Tuple, bool) {
		var u store.ScimUser
		if db.First(&u, "id = ?", id).Error != nil {
			return iam.Tuple{}, false
		}
		return iam.Tuple{User: "user:" + u.Subject(), Relation: "member", Object: groupObj(g.OrgID, g.ID)}, u.Active
	}
	for _, id := range valid {
		if t, active := tuple(id); active && !contains(old, id) {
			add = append(add, t)
		}
	}
	for _, id := range old {
		if t, _ := tuple(id); !contains(valid, id) && t.User != "" {
			del = append(del, t)
		}
	}
	_ = s.FGA.Write(c, add, del)
	s.audit(c, g.OrgID, "scim.group.members", "scim-group:"+g.ID, "success", nil, gin.H{"members": len(valid)}, nil)
}

// syncGroupRoles 는 그룹 이름에 매핑된 역할 튜플(group#member → 역할)을 쓰거나 지운다.
func (s *Server) syncGroupRoles(c *gin.Context, db *gorm.DB, g store.ScimGroup, add bool) {
	if s.FGA == nil {
		return
	}
	var rs []store.ScimGroupRole
	db.Where("org_id = ? AND grp = ?", g.OrgID, g.DisplayName).Find(&rs)
	var ts []iam.Tuple
	for _, r := range rs {
		if t, err := s.memberTuple(g.OrgID, memberRequest{User: groupObj(g.OrgID, g.ID) + "#member", Role: r.Role, Project: r.Project, Env: r.Env}); err == nil {
			ts = append(ts, t)
		}
	}
	if add {
		_ = s.FGA.Write(c, ts, nil)
	} else {
		_ = s.FGA.Write(c, nil, ts)
	}
}

// ---- 그룹 → 역할 매핑 (조직 관리자) ----

func (s *Server) listGroupRoles(c *gin.Context) {
	org := c.Param("org")
	if !s.require(c, "admin", orgObj(org)) {
		return
	}
	s.tenantScope(c, org)
	var rs []store.ScimGroupRole
	s.db(c).Where("org_id = ?", org).Order("grp, role").Find(&rs)
	c.JSON(http.StatusOK, gin.H{"items": rs})
}

func (s *Server) changeGroupRole(c *gin.Context) {
	org := c.Param("org")
	if _, ok := s.orgGuard(c, org); !ok || !s.require(c, "admin", orgObj(org)) {
		return
	}
	var r struct {
		Group   string `json:"group"`
		Role    string `json:"role"`
		Project string `json:"project"`
		Env     string `json:"env"`
	}
	_ = c.ShouldBindJSON(&r)
	if _, err := s.memberTuple(org, memberRequest{User: "group:x#member", Role: r.Role, Project: r.Project, Env: r.Env}); err != nil || r.Group == "" {
		fail(c, http.StatusBadRequest, "bad_request", "group 필수 · "+fmt.Sprint(err), nil)
		return
	}
	s.tenantScope(c, org)
	db := s.db(c)
	m := store.ScimGroupRole{OrgID: org, Grp: r.Group, Role: r.Role, Project: r.Project, Env: r.Env}
	if m.Role != "" && iam.Roles[m.Role][0] == "project" && m.Project == "" {
		m.Project = "default"
	}
	var g store.ScimGroup
	found := db.First(&g, "org_id = ? AND display_name = ?", org, r.Group).Error == nil
	if c.Request.Method == http.MethodDelete {
		if found {
			s.syncGroupRoles(c, db, g, false)
		}
		db.Delete(&m)
		if found {
			s.syncGroupRoles(c, db, g, true) // 남은 매핑 재기록
		}
		s.audit(c, org, "scim.group_role.remove", "group:"+r.Group, "success", nil, m, nil)
		c.Status(http.StatusNoContent)
		return
	}
	if err := db.Save(&m).Error; err != nil {
		fail(c, http.StatusInternalServerError, "internal", err.Error(), nil)
		return
	}
	if found {
		s.syncGroupRoles(c, db, g, true)
	}
	s.audit(c, org, "scim.group_role.set", "group:"+r.Group, "success", nil, m, nil)
	c.JSON(http.StatusOK, m)
}
