# Protocole Shokz Loop120 / OpenComm2

Ce document décrit le protocole de contrôle utilisé entre un PC et le dongle **Shokz Loop120**,
puis entre le dongle et le casque **OpenComm2 2025 Upgrade**, tel qu'implémenté dans
[`shokzctl.py`](../shokzctl.py).

Il a été reconstruit à des fins d'interopérabilité, par analyse statique de l'application
officielle *Shokz Connect* 2.2.4 pour macOS, puis validé sur du vrai matériel :
- les **métadonnées de réflexion Swift** donnent les noms des enums et de leurs cas ;
- les **getters `rawValue`** donnent les valeurs numériques ;
- une **décompilation Ghidra** donne l'encodage des trames.

Aucun code ni aucune ressource Shokz n'est redistribué ici.

> Les valeurs marquées ✅ ont été vérifiées sur un OpenComm2 2025 Upgrade (type `C120`,
> firmware `HC_HU_V_01_20251116`) avec un Loop120 USB-A (firmware `D120_DU_V_03_20241203`).
> Les autres proviennent de l'application et n'ont pas été testées.

---

## 1. Côté USB

| Élément | Valeur |
|---|---|
| Vendor ID | `0x3511` |
| Product ID | `0x2EF2` Loop120 USB-A ✅, `0x2F06` Loop120 USB-C |
| Autres dongles connus | `0x2B0A` Loop110 USB-A, `0x2B1E` Loop110 USB-C (protocole *legacy*, non traité ici) |

Le dongle expose deux interfaces HID en plus de la carte son USB :

| Interface | Usage page | Rôle |
|---|---|---|
| 0 | `0x0C` (Consumer) | Touches téléphonie (décroché, micro coupé…) **et canal de commande** : reports vendeur `0x10`–`0x17` |
| 1 | `0xFF00` (vendeur) | Mise à jour firmware (DFU) : reports `3`, `5`, `6`. **freeShokz n'y écrit jamais.** |

Sous Linux, l'interface 0 correspond à un nœud `/dev/hidrawN`. On l'identifie par
`HID_ID=0003:00003511:00002EF2` et `bInterfaceNumber == 00`.

### Report IDs de l'interface 0

Chaque report fait 255 octets. Les noms sont ceux de l'enum `PackageMessageReportId` de l'appli.

| ID | Sens | Nom | Usage |
|---|---|---|---|
| `0x10` | PC → | `toHSD` | Ancien OpenComm2 (protocole *legacy*) |
| `0x11` | → PC | `fromHSD` | |
| `0x12` | PC → | `toDongle` | Commandes adressées au dongle ✅ |
| `0x13` | → PC | `fromDongle` | Réponses et notifications du dongle ✅ |
| `0x14` | PC → | `toHCVA` | Commandes relayées au casque (OpenComm2 2025, OpenMeet) ✅ |
| `0x15` | → PC | `fromHCVA` | Réponses et notifications du casque ✅ |

L'envoi se fait par un *output report* : une simple écriture `write()` sur le hidraw. Le premier
octet est le report ID. freeShokz complète la trame avec des zéros jusqu'à 1 + 255 octets.

---

## 2. Format d'une trame

```
report | LinkData                      | TransPortData          | TLV
  1    | A5 5A | len u16 | 01 01 00 00 | tlv_len u16 | crc u16  | ...
```

Tous les entiers sont en **little-endian**.

- **`LinkData`** :
  - le magic `0x5AA5`, qui apparaît sur le fil sous la forme `A5 5A` ;
  - `len` = `tlv_len + 4` ;
  - quatre octets constants `01 01 00 00`.
- **`TransPortData`** : la longueur du TLV, puis son CRC.
- **CRC** : **CRC-16/MAXIM**, calculé sur le TLV seul.
  - polynôme `0x8005` réfléchi (`0xA001`), init `0x0000`, xorout `0xFFFF` ;
  - un TLV vide donne `0xFFFF` ;
  - vecteur de test : `"123456789"` → `0x44C2`.

### TLV (version « v120 »)

Dix champs `u32` suivis de la valeur :

```
T1=1  L1=0x20+n   T2=2  L2=4  tag1   T3=3  L3=4  tag2   T4=4  L4=n   value[n]
```

- `L1` couvre tout ce qui suit la paire `T1`/`L1`.
- `tag1` désigne la cible et la méthode, `tag2` la commande.

La variante « v110 » du Loop110 code `tag1` et `tag2` sur 2 octets ; elle n'est pas implémentée.

### Valeurs de `tag1` (`ShokzTag1`)

| Cible | set | get | sync (notification) |
|---|---|---|---|
| Casque (`appXxxHeadset`) | `0x01` | `0x02` | `0x03` |
| Dongle (`pcXxxDongle`) | `0x10` | `0x11` | `0x12` |

`dongleOTA` existe aussi (mise à jour firmware) ; freeShokz ne l'utilise pas.

### Réponses et notifications

- Une **réponse** reprend le `tag1` de la requête, et son `tag2` porte le bit `0x8000`
  (`get` `0x03` → réponse `0x8003`).
- Une réponse du **casque** commence par un octet de statut (`0x00` = OK), suivi des données.
  Une réponse du **dongle** contient directement les données.
- Une **notification** spontanée utilise `tag1 = 0x03` (casque) ou `0x12` (dongle), et un `tag2`
  sans le bit `0x8000`.

### Exemple : lecture de la batterie ✅

```
TX 14 a5 5a 2c 00 01 01 00 00 28 00 a7 74
   01 00 00 00 20 00 00 00  02 00 00 00 04 00 00 00 02 00 00 00
   03 00 00 00 04 00 00 00 03 00 00 00  04 00 00 00 00 00 00 00

RX 15 a5 5a 30 00 01 01 00 00 2c 00 3d 0e
   01 00 00 00 24 00 00 00  02 00 00 00 04 00 00 00 02 00 00 00
   03 00 00 00 04 00 00 00 03 80 00 00  04 00 00 00 04 00 00 00
   00 09 ff 00
```

Lecture de la réponse : statut `00`, niveau `09`, donc (9 + 1) × 10 = **100 %**.

---

## 3. Ouvrir le canal vers le casque (SPP)

Les commandes casque (`0x14`) restent **sans réponse** tant que le dongle n'a pas ouvert un canal
SPP avec le casque. Shokz Connect fait cette ouverture à chaque démarrage (`Dongle120.startWorkFlow`).

1. **Casque connecté ?** Dongle `get 0x09`. La réponse `01` signifie que le casque est connecté en
   Bluetooth. ✅
2. **Canal déjà ouvert ?** Dongle `get 0x07` avec pour valeur l'UUID en `u32` (`f0 fe 00 00`). ✅
   - Réponse : `[n] [ordre] [statut] [adresse ×6] ...`. Un statut `01` signifie que le canal est
     ouvert.
   - Exemple : `01 00 01 b8 84 11 15 74 38 …`.
3. **Adresse du casque** : dongle `get 0x06` (historique d'appairage). ✅
   - Réponse : `[n] [?] [?] [adresse ×6, LE] [taille nom] [nom]`.
   - Les octets du nom arrivent **à l'envers** : `II_zkohS yb 2mmoCnepO` se lit
     `OpenComm2 by Shokz_II`.
4. **Ouverture** : dongle `set 0x05`, valeur = `adresse ×6` + `uuid u16`
   (`b8 84 11 15 74 38 f0 fe`). ✅
   - Le dongle acquitte par `0x8005`, en renvoyant la valeur.
   - Il envoie ensuite une notification `sync 0x02` (état du SPP) terminée par `01`.

L'UUID dépend du type de casque (table de `Dongle120.createSppConnect`) :

| HeadsetType | Casques | UUID SPP |
|---|---|---|
| `HC` / `VA` | OpenComm2 2025, OpenMeet | `0xFEF0` ✅ |
| `HSD` | ancien OpenComm2 | `0x1101` |
| `IWE` | filaire | `0xFC4A` |

---

## 4. Commandes du dongle (Loop120)

### `get` (`tag1 = 0x11`)

| tag2 | Nom | Format de la réponse |
|---|---|---|
| `0x01` | getBluetoothAddress | adresse sur 6 octets (LE) + bourrage ✅ |
| `0x03` | getAutoSearchConnectStatus | |
| `0x04` | getRemoteControlStatus | |
| `0x05` | getRingtoneStatus | |
| `0x06` | getPairingHistory | voir §3 ✅ |
| `0x07` | getSppConnectionStatus | valeur = UUID `u32`, voir §3 ✅ |
| `0x08` | getCurrentMode | `00` ✅ |
| `0x09` | getBluetoothConnectionStatus | `01` = connecté ✅ |
| `0x0A` | getVersionInfo | ASCII, ex. `D120_DU_V_03_20241203` ✅ |
| `0x0C` | getConnectedHeadsetType | ASCII, ex. `C120` ✅ |

### `set` (`tag1 = 0x10`)

La valeur de chaque commande est son rang dans l'enum + 1 :

| tag2 | Nom | | tag2 | Nom |
|---|---|---|---|---|
| `0x01` | enterScan | | `0x08` | disconnectDevice |
| `0x02` | leaveScan | | `0x09` | enterOTAMode ⚠️ |
| `0x03` | setAutoSearchConnect | | `0x0A` | exitOTAMode |
| `0x04` | setRemoteControl | | `0x0B` | setRingtone |
| `0x05` | sppConnect ✅ | | `0x0C` | factoryReset ⚠️ |
| `0x06` | sppDisconnect | | `0x0D` | pairWithDevice |
| `0x07` | connectDevice | | `0x0E` | deletePairedDevice |

### Notifications (`tag1 = 0x12`)

| tag2 | Nom |
|---|---|
| `0x01` | listenBluetoothConnectionStatus |
| `0x02` | listenSppConnectionStatus ✅ |
| `0x03` | listenScanResults |
| `0x04` | listenPairingResult |

---

## 5. Commandes du casque (HCVA)

### `get` (`tag1 = 0x02`) : `PcGetComm3_Tag2`

L'OpenComm2 2025 ne répond qu'aux six commandes marquées ✅. Les autres, propres aux
OpenComm3 et OpenMeet, restent sans réponse.

| tag2 | Nom | | tag2 | Nom |
|---|---|---|---|---|
| `0x02` | getDeviceVersionName ✅ | | `0x21` | getDeviceBluetoothAddress |
| `0x03` | getDeviceBatteryLevel ✅ | | `0x2F` | getHeadsetBrandCompatibility |
| `0x04` | getDevicePairingList ✅ | | `0x31` | getHeadsetInstancePairingOpen |
| `0x06` | getDeviceLanguage ✅ | | `0x43` | getHeadsetCustomEQ |
| `0x08` | getHeadsetEQ ✅ | | `0x44` | getHeadsetBluetoothName |
| `0x10` | getDeviceMutConn ✅ | | `0x71` | getVADDetect |
| `0x11` | getDeviceAlertSoundLevel | | `0x72` | getHeadsetCallEQ |
| `0x12` | getDeviceEnableKeyClickSound | | `0x73` | getHeadsetCallCustomEQ |
| `0x13` | getDeviceEnableAutoRejectCalls | | `0x74` | getHeadsetMuteLever |
| `0x14` | getDeviceBusylightCalls | | `0x75` | getIncomingCallRingtoneType |
| `0x15` | getDeviceEnableMicrophoneMuteReminder | | `0x76` | getIncomingCallRingtoneVolume |
| `0x16` | getDeviceEnableComputerAudioPriority | | `0x77` | getMicNoiseReductionLevel |
| `0x17` | getDeviceSleepTime | | `0x78` | getVolumeKnobDirection |
| `0x18` | getDeviceAlertSoundType | | | |
| `0x19` | getDeviceChargingStatus | | | |
| `0x1E` | getDeviceCallingStatus | | | |

### `set` (`tag1 = 0x01`) : `PcSetComm3_Tag2`

| tag2 | Nom | | tag2 | Nom |
|---|---|---|---|---|
| `0x02` | setHeadsetLanaguage | | `0x1D` | setHeadsetEnterPairing |
| `0x09` | setDeleteDevice | | `0x1E` | setControlPlayTestAudio |
| `0x0B` | setConnectDevice | | `0x23` | setHeadsetLeavePairing |
| `0x0C` | setDisConnectDevice | | `0x33` | setHeadsetBrandCompatibility |
| `0x0E` | **setHeadsetEQ** ✅ | | `0x35` | setHeadsetInstancePairingOpen |
| `0x10` | setCloseDeviceMutConn | | `0x51` | setVADDetect |
| `0x11` | setOpenDeviceMutConn | | `0x52` | setHeadsetCallEQ |
| `0x12` | setFactoryReset ⚠️ | | `0x53` | setCustomHeadsetCallEQ |
| `0x15` | setDeviceAlertSoundLevel | | `0x54` | setHeadsetMuteLever |
| `0x16` | setDeviceEnableKeyClickSound | | `0x55` | setIncomingCallRingtoneType |
| `0x17` | setDeviceEnableAutoRejectCalls | | `0x56` | setIncomingCallRingtoneVolume |
| `0x18` | setDeviceBusylightCalls | | `0x57` | setIncomingCallRingtoneTypePlayTestAudio |
| `0x19` | setDeviceEnableMicrophoneMuteReminder | | `0x58` | setMicNoiseReductionLevel |
| `0x1A` | setDeviceEnableComputerAudioPriority | | `0x59` | setVolumeKnobDirection |
| `0x1B` | setDeviceSleepTime | | `0x63` | setCustomHeadsetEQ |
| `0x1C` | setDeviceAlertSoundType | | `0x64` | setHeadsetBluetoothName |

### Notifications (`tag1 = 0x03`) : `PcSyncComm3_Tag2`

| tag2 | Nom |
|---|---|
| `0x01` | syncCurrentAllStatus |
| `0x02` | syncCurrentChargingStatus |
| `0x03` | syncCurrentBatteryLevel |
| `0x09` | syncCurrentHeadsetEQ |
| `0x0B` | syncCurrentLinkStatus |

Le contenu de ces notifications n'est pas décodé : à la réception, freeShokz relit simplement le
paramètre concerné.

---

## 6. Formats des valeurs (casque)

Les exemples ci-dessous sont des valeurs complètes, statut compris.

| Commande | Exemple | Interprétation |
|---|---|---|
| Batterie `0x03` | `00 09 ff 00` | niveau `0`–`9` → % = (niveau + 1) × 10 (`updateDeviceBatteryLevel`). Le `ff` n'est pas identifié, peut-être l'état de charge. ✅ |
| Version `0x02` | `00 48 43 5f …` | ASCII terminé par NUL : `HC_HU_V_01_20251116` ✅ |
| Langue `0x06` | `00 00` | 0 anglais, 1 chinois, 2 japonais, 3 coréen, 4 français, 5 allemand, 6 espagnol ✅ |
| EQ `0x08` | `00 02 00 00 00 00 00 00` | `EQMode2` : 1 standard, 2 voix renforcée, 3 bass boost, 4 treble boost, 5–6 personnalisé, 7 natation, 8 conduction osseuse ✅ |
| Multipoint `0x10` | `00 01 01` | 1er octet = multipoint activé ✅ |
| Appairages `0x04` | `00 02 …` | `[n]` puis n × `{connecté u8, adresse ×6 LE, u8, taille nom u8 (0x1F), nom ASCII bourré de NUL}` ✅ |

### Écrire l'EQ ✅

`set 0x0E`, avec pour valeur `[mode] 00 00 00 00 00 00 00` (8 octets). Par exemple
`02 00 00 00 00 00 00 00` passe en voix renforcée.

Le casque confirme par un statut `00`, puis diffuse une notification `syncCurrentHeadsetEQ`. On
entend la bascule dans le casque.

### Langue : non implémentée

Dans Shokz Connect, le changement de langue passe par une fenêtre de progression
(`LanguageUpdateProgressAlertView`). Il semble donc téléverser un pack d'invites vocales.
freeShokz s'abstient tant que ce n'est pas mieux compris.
