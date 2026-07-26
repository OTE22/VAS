# Promote to Known and Merge Functionality Guide

## Table of Contents
1. [Overview](#overview)
2. [Promote to Known](#promote-to-known)
3. [Merge Identities](#merge-identities)
4. [Examples](#examples)
5. [Best Practices](#best-practices)

---

## Overview

The Face Recognition System provides two powerful features for managing unknown identities:

1. **Promote to Known**: Convert an unknown identity to a known identity by assigning a display name
2. **Merge Identities**: Combine multiple identities (usually duplicates) into a single identity

Both features are available in the **Unknown Faces** admin page (`/admin/unknown`).

**Note for Regular Users:** If you have been granted pipeline access by an administrator, you can also promote and merge identities from your assigned pipelines. See **26_USER_PIPELINE_ACCESS_GUIDE.md** for details.

---

## Promote to Known

### What is "Promote to Known"?

When the system detects a face that doesn't match any known person, it creates an "Unknown" identity. The **Promote to Known** feature allows you to:

- Assign a display name to an unknown identity
- Convert it from "unknown" type to "known" type
- Make it searchable and recognizable in future detections
- Move it from the Unknown Faces page to the known identities database

### When to Use Promote to Known

Use this feature when:
- ✅ You have identified an unknown person and know their name
- ✅ You want to add them to the known persons database
- ✅ You want future detections of this person to be automatically recognized
- ✅ The identity represents a single, unique person

### How to Promote an Unknown Identity

#### Step 1: Navigate to Unknown Faces Page
1. Go to `/admin/unknown` in your browser
2. Browse through the unknown identities grouped by pipeline
3. Find the identity you want to promote

#### Step 2: View Identity Details
1. Click on the identity card to view details
2. Review the identity information:
   - Appearances count
   - First seen / Last seen dates
   - Appearance timeline
   - Best snapshot image

#### Step 3: Promote the Identity
1. Click the **"PROMOTE TO KNOWN"** button in the detail modal
2. A promotion form will appear
3. Enter the **Display Name** (required):
   - Example: "John Doe", "Jane Smith", "Employee-123"
   - This will be the name shown in future detections
4. Optionally enter a **Person Code**:
   - Example: Employee ID, badge number, or any unique identifier
   - This is useful for tracking purposes
5. Click **"PROMOTE"** to confirm

#### Step 4: Verification
- The identity will be removed from the Unknown Faces page
- It will now appear in the known identities database
- Future detections of this person will automatically show their name
- The identity type changes from "unknown" to "known"

### Example: Promoting an Unknown Identity

**Scenario**: You see an unknown person detected multiple times at the main entrance. After reviewing security footage, you identify them as "John Smith", an employee with badge number "EMP-456".

**Steps**:
1. Open Unknown Faces page: `/admin/unknown`
2. Find the unknown identity (grouped by pipeline "MAIN-ENTRANCE")
3. Click on the identity card to view details
4. Click **"PROMOTE TO KNOWN"**
5. Enter:
   - Display Name: `John Smith`
   - Person Code: `EMP-456`
6. Click **"PROMOTE"**

**Result**: 
- Identity is now known as "John Smith"
- Future detections will show "John Smith" instead of "Unknown"
- The identity is searchable in the system

---

## Merge Identities

### What is "Merge Identities"?

The **Merge Identities** feature allows you to combine two or more identities into a single identity. This is useful when:

- The same person has been detected as multiple separate identities (duplicates)
- You want to consolidate detection history
- You want to merge unknown identities before promoting them

### When to Use Merge Identities

Use this feature when:
- ✅ The same person appears as multiple different identities
- ✅ You want to combine their detection history
- ✅ You want to merge multiple unknown identities into one before promoting
- ✅ You want to consolidate duplicate entries

**⚠️ Important**: Merging is **irreversible**. Make sure both identities represent the same person before merging.

### How to Merge Identities

#### Step 1: Open Merge Modal
1. Go to `/admin/unknown` page
2. Click on an identity card to view details
3. Click the **"MERGE"** button in the detail modal

#### Step 2: Select Source Identity
- The **"From Identity ID"** field is automatically filled with the current identity
- This is the identity that will be merged INTO the target

#### Step 3: Find Target Identity
1. Enter the target identity ID in the **"To Identity ID (Target)"** field
   - You can enter a full UUID or search for it
2. Click **"SEARCH"** to find and preview the target identity
3. Review the search results:
   - Identity name/type
   - Appearances count
   - First seen date
   - Preview image
4. Click **"SELECT"** to choose the target identity

#### Step 4: Add Notes (Optional)
- Add any notes about why you're merging these identities
- Example: "Same person detected with different angles", "Duplicate entries"

#### Step 5: Execute Merge
1. Review both identity IDs to ensure they're correct
2. Click **"MERGE"** to confirm
3. Confirm the merge action in the popup dialog

#### Step 6: Verification
- The source identity is merged into the target identity
- All appearances, faces, and embeddings are transferred
- The source identity is removed from the system
- The target identity now contains all combined data

### Example 1: Merging Duplicate Unknown Identities

**Scenario**: The same unknown person has been detected multiple times, but the system created two separate identities:
- Identity A: `422240e1-29ff-4004-8b29-7008c98da09f` (5 appearances)
- Identity B: `a1b2c3d4-e5f6-7890-abcd-ef1234567890` (3 appearances)

You want to merge them into one identity before promoting.

**Steps**:
1. Open Unknown Faces page
2. Click on Identity A to view details
3. Click **"MERGE"** button
4. In the merge modal:
   - From Identity ID: `422240e1-29ff-4004-8b29-7008c98da09f` (auto-filled)
   - To Identity ID: `a1b2c3d4-e5f6-7890-abcd-ef1234567890`
5. Click **"SEARCH"** to verify Identity B
6. Review the preview and click **"SELECT"**
7. Add note: "Same person, duplicate detections"
8. Click **"MERGE"** and confirm

**Result**:
- Identity B now has 8 appearances (5 + 3)
- Identity A is removed
- You can now promote Identity B to known with a single name

### Example 2: Merging Unknown into Known

**Scenario**: You have a known identity "John Doe" and an unknown identity that you've identified as the same person. You want to merge the unknown into the known identity.

**Steps**:
1. Open Unknown Faces page
2. Find the unknown identity
3. Click on it to view details
4. Click **"MERGE"** button
5. In the merge modal:
   - From Identity ID: `<unknown-identity-id>` (auto-filled)
   - To Identity ID: `<john-doe-identity-id>` (search for "John Doe" or use his ID)
6. Click **"SEARCH"** to find John Doe's identity
7. Verify it's the correct person
8. Click **"SELECT"**
9. Add note: "Unknown detections belong to John Doe"
10. Click **"MERGE"** and confirm

**Result**:
- John Doe's identity now includes all the unknown detections
- The unknown identity is removed
- All detection history is consolidated

### Example 3: Using Merge Suggestions

The system can automatically suggest identities that should be merged based on similarity.

**Access Note:** Regular users with pipeline access can also view and manage merge suggestions for their assigned pipelines.

**Steps**:
1. Go to **Admin → Unknown Faces** (or **UNKNOWN FACES** for regular users with pipeline access)
2. Click **"MERGE SUGGESTIONS"** button in the page header
3. Review the suggested merges:
   - Each suggestion shows confidence percentage
   - Lists identity IDs to be merged
   - Shows representative snapshots
   - **Regular Users**: Only see suggestions for identities from your assigned pipelines
4. For each suggestion:
   - Click **"APPROVE"** to automatically merge all identities in the suggestion
   - Click **"REJECT"** to dismiss the suggestion
5. The system will merge identities with high confidence automatically

**Example Suggestion**:
```
Cluster 1
Confidence: 95.3%
Identities to merge: 3
[Preview images of 3 similar faces]
```

Clicking **"APPROVE"** will merge all 3 identities into one.

---

## Best Practices

### For Promote to Known

1. **Verify Identity First**
   - Review multiple appearances before promoting
   - Check the appearance timeline
   - Ensure it's a single, unique person

2. **Use Clear Display Names**
   - Use full names: "John Doe" not "John" or "JD"
   - Be consistent with naming conventions
   - Include titles if relevant: "Dr. Jane Smith"

3. **Add Person Codes When Available**
   - Employee IDs, badge numbers, etc.
   - Helps with tracking and reporting
   - Makes it easier to identify people in exports

4. **Promote After Multiple Detections**
   - Wait for at least 2-3 appearances to confirm it's the same person
   - Review the best snapshot quality
   - Ensure the face is clearly visible

### For Merge Identities

1. **Always Verify Before Merging**
   - Review both identities' snapshots
   - Check appearance timelines
   - Ensure they're the same person
   - **Merging is irreversible!**

2. **Merge Unknown Before Promoting**
   - If you have multiple unknown identities of the same person
   - Merge them first, then promote the consolidated identity
   - This prevents duplicate known identities

3. **Use Merge Suggestions Wisely**
   - Review high-confidence suggestions (90%+)
   - Manually verify medium-confidence suggestions (70-90%)
   - Reject low-confidence suggestions (<70%)

4. **Add Notes for Audit Trail**
   - Document why you're merging
   - Helps with future reference
   - Useful for troubleshooting

5. **Merge Order Matters**
   - Merge smaller identities into larger ones
   - Merge unknown into known (not vice versa)
   - Keep the identity with better quality snapshots

---

## API Endpoints (For Developers)

### Promote to Known
```http
POST /api/admin/unknown/{identity_id}/promote
Authorization: Bearer <admin_token>
Content-Type: application/json

{
  "display_name": "John Doe",
  "person_code": "EMP-456"
}
```

### Merge Identities
```http
POST /api/admin/identities/merge
Authorization: Bearer <admin_token>
Content-Type: application/json

{
  "from_identity_id": "422240e1-29ff-4004-8b29-7008c98da09f",
  "to_identity_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "notes": "Same person, duplicate detections"
}
```

### Get Merge Suggestions

**Access:** Admin or users with pipeline access (filtered to their assigned pipelines)

```http
GET /api/admin/merge-suggestions
Authorization: Bearer <token>
```

**Response for Regular Users:** Only returns merge suggestions for identities from their assigned pipelines

### Approve Merge Suggestion

**Access:** Admin or users with pipeline access (only for suggestions involving their assigned pipelines)

```http
POST /api/admin/merge-suggestions/{suggestion_id}/approve
Authorization: Bearer <token>
```

**Note for Regular Users:** You can only approve suggestions where all identities involved are from your assigned pipelines

---

## Troubleshooting

### Issue: "Identity not found" when searching for merge target
**Solution**: 
- Verify the identity ID is correct
- Ensure the identity exists in the system
- Check if you're searching for a known identity (use full UUID)

### Issue: Cannot merge identities
**Solution**:
- Ensure both identities exist
- Check that you're not trying to merge an identity with itself
- Verify you have admin permissions or pipeline access to both identities
- For regular users: Both identities must be from your accessible pipelines

### Issue: Promoted identity still shows as unknown
**Solution**:
- Refresh the page
- Check the identity type in the database
- Verify the promotion was successful (check logs)

### Issue: Merge suggestions not appearing
**Solution**:
- The system needs sufficient data to generate suggestions
- Wait for more detections to accumulate
- Suggestions are generated periodically, not in real-time

---

## Summary

- **Promote to Known**: Convert unknown → known by assigning a name
- **Merge Identities**: Combine multiple identities into one
- **Always verify** before promoting or merging
- **Use merge suggestions** for automatic duplicate detection
- **Add notes** for audit trail and future reference

For more information, contact your system administrator.

