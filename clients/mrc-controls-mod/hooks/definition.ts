// 由 `python -m cloudyfan_jev.controls definition --typescript` 產生；不要手改。
export const DEFINITION = {
  "fields": {
    "escalation": {
      "command": "/升級",
      "label": "升級",
      "tags": {
        "#不升級": "off"
      },
      "values": {
        "auto": "自動升級",
        "off": "不升級"
      },
      "words": {
        "開": null,
        "關": "off"
      }
    },
    "max_usd": {
      "command": "/上限",
      "label": "上限",
      "tag_with_argument": "#上限",
      "tags": {},
      "values": {},
      "words": {
        "無": null
      }
    },
    "risk": {
      "command": "/風險",
      "label": "風險",
      "tags": {
        "#一般風險": "normal",
        "#高風險": "high"
      },
      "values": {
        "high": "高",
        "normal": "一般"
      },
      "words": {
        "一般": "normal",
        "高": "high"
      }
    },
    "route": {
      "command": "/路徑",
      "label": "路徑",
      "tags": {
        "#路徑一": "path1",
        "#路徑二": "path2"
      },
      "values": {
        "path1": "路徑一（可驗收）",
        "path2": "路徑二（開放題）"
      },
      "words": {
        "一": "path1",
        "二": "path2",
        "自動": null
      }
    },
    "setting": {
      "command": "/模型",
      "label": "模型",
      "tag_with_argument": "#模型",
      "tags": {},
      "values": {},
      "words": {
        "自動": null
      }
    }
  },
  "high_risk_note": "改用高風險時，開放題由顧問派送時改用 Fable 5.2 high，作答花費約為一般（Sonnet 5.5 xhigh）的 2.5 倍（2026-11-26 盲測的牌價計算值，未和帳單核對）；顧問自己回答時不變。",
  "max_usd_presets": [
    "0.10",
    "0.50",
    "1.00"
  ],
  "risk_checklist": [
    {
      "id": "irreversible",
      "keywords": [
        "刪除",
        "刪掉",
        "不可逆",
        "無法復原",
        "覆寫",
        "清空",
        "下架",
        "drop table",
        "rm -rf",
        "force push"
      ],
      "label": "難以回頭"
    },
    {
      "id": "costly",
      "keywords": [
        "採購",
        "下單",
        "付款",
        "預算",
        "合約金額",
        "報價",
        "投資"
      ],
      "label": "花費大"
    },
    {
      "id": "public",
      "keywords": [
        "發布",
        "公告",
        "新聞稿",
        "上線",
        "部署",
        "推送",
        "寄給",
        "對外"
      ],
      "label": "對外發布"
    },
    {
      "id": "customer",
      "keywords": [
        "客戶",
        "住戶",
        "家屬",
        "使用者資料",
        "個資"
      ],
      "label": "牽涉客戶"
    },
    {
      "id": "legal_safety",
      "keywords": [
        "法規",
        "合規",
        "契約",
        "授權書",
        "醫療",
        "用藥",
        "跌倒",
        "安全",
        "資安",
        "漏洞"
      ],
      "label": "法規或安全"
    }
  ],
  "suggested_settings": [
    "sonnet-xhigh",
    "fable-high",
    "haiku-low"
  ],
  "version": "mrc-user-controls-v1"
} as const
