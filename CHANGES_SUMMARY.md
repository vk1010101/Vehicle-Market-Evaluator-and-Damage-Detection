# Changes Summary - UI and Logic Updates

## Overview
This document summarizes all the changes made to implement the user's requirements for:
1. Removing specific form fields
2. Changing Year fields to single field
3. Changing CASEID to Quotation ID
4. Adding checkboxes for individual module execution
5. Implementing individual module execution logic

## Files Modified

### 1. `templates/index.html`
**Changes Made:**
- ✅ Removed "Engine number" field
- ✅ Removed "Plate number" field  
- ✅ Removed "Plate character" field
- ✅ Changed "Year (from)" and "Year (To)" to single "Year" field
- ✅ Changed "Case ID" to "Quotation ID"
- ✅ Added checkboxes for "Google Image check" and "Insurance Lookup" at the top
- ✅ Updated JavaScript validation to remove year range validation
- ✅ Updated placeholder text for Quotation ID

**Form Field Changes:**
```html
<!-- BEFORE: -->
<input name="year_min" placeholder="No Min">
<input name="year_max" placeholder="No Max">
<input name="engine_no" placeholder="eg. VC10338">
<input name="platenumber" placeholder="eg. 1038">
<input name="charachternumber" placeholder="eg. GH">

<!-- AFTER: -->
<input name="year" placeholder="e.g., 2020">
<!-- Engine, Plate, and Character fields removed -->

<!-- NEW: -->
<input type="checkbox" name="google_image_check" value="1"> Google Image Check
<input type="checkbox" name="insurance_lookup" value="1"> Insurance Lookup
```

### 2. `templates/results.html`
**Changes Made:**
- ✅ Changed "CaseID:" to "Quotation ID:" in display
- ✅ Updated JavaScript function parameter from `caseId` to `quotationId`
- ✅ Updated function calls to use new parameter name

**Display Changes:**
```html
<!-- BEFORE: -->
<span class="text-gray-700 font-medium mr-2">CaseID:</span>

<!-- AFTER: -->
<span class="text-gray-700 font-medium mr-2">Quotation ID:</span>
```

### 3. `app.py`
**Changes Made:**

#### Search Function (`/search` route):
- ✅ Added year parsing logic: single "year" field → `year_min` and `year_max`
- ✅ Removed references to removed fields (`engine_no`, `plate_no`, `charachter`)
- ✅ Added checkbox field processing (`google_image_check`, `insurance_lookup`)
- ✅ Updated individual module execution logic:
  - Google Image check only runs if checkbox is selected AND chassis number exists
  - Insurance Lookup only runs if checkbox is selected AND chassis number exists
- ✅ Moved database storage calls to individual module execution blocks

#### Database Functions:
- ✅ Updated `create_user_session()` to remove unused columns
- ✅ Added new columns: `GoogleImageCheck`, `InsuranceLookup`
- ✅ Updated INSERT statement to match new schema

#### Refresh Source Function (`/refresh_source` route):
- ✅ Added year parsing logic for individual source refreshes
- ✅ Enhanced individual module execution:
  - Google Image refresh with checkbox validation
  - Insurance Lookup refresh with checkbox validation
- ✅ Added database storage and logging for individual module refreshes
- ✅ Fixed indentation issues in year parsing logic

**Code Changes:**
```python
# Year parsing logic added:
year_value = request.form.get("year")
year_min = year_max = None
if year_value:
    try:
        year_int = int(year_value)
        year_min = year_int
        year_max = year_int
    except ValueError:
        pass

# Individual module execution:
if crit.get("google_image_check") and crit.get("chasis_no"):
    # Run Google Image check
if crit.get("insurance_lookup") and crit.get("chasis_no"):
    # Run Insurance Lookup
```

### 4. `update_schema.sql`
**New File Created:**
- ✅ SQL script to update existing database schema
- ✅ Removes unused columns: `EngineNumber`, `PlateNumber`, `Character`
- ✅ Adds new columns: `GoogleImageCheck`, `InsuranceLookup`
- ✅ Sets default values for existing records

## Database Schema Changes

### `v_UserSession` Table:
**Columns Removed:**
- `EngineNumber` (NVARCHAR(50))
- `PlateNumber` (NVARCHAR(50)) 
- `Character` (NVARCHAR(10))

**Columns Added:**
- `GoogleImageCheck` (BIT DEFAULT 0)
- `InsuranceLookup` (BIT DEFAULT 0)

## New Functionality

### Individual Module Execution:
1. **Google Image Check**: Only runs when checkbox is selected AND chassis number provided
2. **Insurance Lookup**: Only runs when checkbox is selected AND chassis number provided
3. **Damage Detection**: Remains as checkbox-controlled functionality
4. **Scrapers**: Can be individually selected via checkboxes

### Year Field Handling:
- Single "Year" input field in UI
- Automatically parsed to both `year_min` and `year_max` for scraping compatibility
- Maintains backward compatibility with existing scraper logic

## Benefits of Changes

1. **Cleaner UI**: Removed unnecessary fields that weren't being used
2. **Better UX**: Single year field is more intuitive than year range
3. **Individual Control**: Users can now run only the modules they need
4. **Performance**: Avoids running unnecessary checks when not requested
5. **Database Efficiency**: Removes unused columns and adds meaningful flags
6. **Consistent Naming**: "Quotation ID" is more professional than "Case ID"

## Testing Recommendations

1. **Form Submission**: Test with various year values (single year, empty, invalid)
2. **Individual Modules**: Test each checkbox independently
3. **Database Integration**: Verify new schema columns are created correctly
4. **Backward Compatibility**: Ensure existing sessions still work
5. **Refresh Functionality**: Test individual source refreshes with new logic

## Next Steps

1. Run `update_schema.sql` in SSMS to update database schema
2. Test the application with new form structure
3. Verify individual module execution works as expected
4. Test year parsing with various input values
5. Validate database storage for new checkbox fields
